"""KPI engine: the catalog in config/kpis.yaml, evaluated over what a user may see.

``compute()`` is the only way the dashboard and the alerts get numbers. It runs each KPI's
aggregate over ``security.relation()`` — so row-level security applies to every figure — then
filters and groups. Ratios are computed as Σ/Σ over the selected rows, so a region's OTD and the
national OTD come from the same formula.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import yaml

from bi_kpi.security import User, relation

CURRENCIES = ("BRL", "MXN", "USD")
UNITS = ("currency", "count", "ratio", "days", "score")
DIRECTIONS = ("up", "down", "none")

# Filters and groupings: name -> column. Every dataset has the first group; the second exists
# only at order-line grain, so on order and review datasets it filters through the lines.
COMMON_DIMS = {
    "month": "year_month",
    "region": "customer_region",
    "state": "customer_state",
    "payment_type": "payment_type",
}
LINE_DIMS = {"category": "category", "seller": "seller_id"}

# The sales plan exists per customer region and month, nothing finer.
PLAN_DIMS = ("month", "region")


@dataclass(frozen=True)
class Kpi:
    id: str
    name: str
    group: str
    unit: str
    direction: str
    dataset: str | None = None
    expr: str | None = None
    plan_of: str | None = None
    target: float | None = None

    def __post_init__(self) -> None:
        if self.unit not in UNITS or self.direction not in DIRECTIONS:
            raise ValueError(f"{self.id}: bad unit or direction")
        if (self.expr is None) == (self.plan_of is None):
            raise ValueError(f"{self.id}: needs exactly one of expr / plan_of")
        if self.expr is not None and self.dataset is None:
            raise ValueError(f"{self.id}: expr needs a dataset")

    def status(self, value: float | None) -> str | None:
        """'ok' / 'off_target' against the target, None when there is nothing to compare."""
        if value is None or pd.isna(value) or self.target is None or self.direction == "none":
            return None
        good = value >= self.target if self.direction == "up" else value <= self.target
        return "ok" if good else "off_target"


def load_kpis(path: Path) -> dict[str, Kpi]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["kpis"]
    kpis = {k["id"]: Kpi(**k) for k in raw}
    for k in kpis.values():
        if k.plan_of and kpis.get(k.plan_of) is None:
            raise ValueError(f"{k.id}: plan_of {k.plan_of!r} is not a KPI")
    return kpis


@dataclass(frozen=True)
class TargetSettings:
    monthly_growth: float
    lookback_months: int
    overrides: tuple[dict, ...] = ()


def load_targets(path: Path) -> TargetSettings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return TargetSettings(
        monthly_growth=float(raw["monthly_growth"]),
        lookback_months=int(raw["lookback_months"]),
        overrides=tuple(raw.get("overrides") or ()),
    )


def build_targets(con: duckdb.DuckDBPyConnection, settings: TargetSettings) -> int:
    """mart.fact_targets: monthly sales plan per customer region (see config/targets.yaml)."""
    n = settings.lookback_months
    con.execute(
        f"""
        CREATE OR REPLACE TABLE mart.fact_targets AS
        WITH months AS (
            SELECT DISTINCT year_month, date_trunc('month', date) AS month, is_complete_month
            FROM mart.dim_date
        ),
        regions AS (SELECT DISTINCT region FROM mart.dim_region),
        sales AS (
            SELECT year_month, customer_region AS region, sum(price_brl) AS gmv
            FROM mart.v_sales WHERE NOT is_canceled GROUP BY ALL
        ),
        grid AS (
            SELECT m.year_month, m.month, m.is_complete_month, r.region,
                   coalesce(s.gmv, 0) AS gmv
            FROM months m CROSS JOIN regions r
            LEFT JOIN sales s ON s.year_month = m.year_month AND s.region = r.region
        ),
        run_rate AS (
            SELECT *,
                   avg(gmv) OVER w                                         AS base,
                   count(*) FILTER (WHERE is_complete_month) OVER w        AS complete_base
            FROM grid
            WINDOW w AS (PARTITION BY region ORDER BY month
                         ROWS BETWEEN {n} PRECEDING AND 1 PRECEDING)
        )
        SELECT year_month, region,
               CAST(round(base * (1 + ?), 2) AS DECIMAL(14, 2)) AS target_brl,
               'run_rate' AS method
        FROM run_rate
        WHERE is_complete_month AND complete_base = {n}
        """,
        [settings.monthly_growth],
    )
    for o in settings.overrides:
        con.execute(
            "DELETE FROM mart.fact_targets WHERE year_month = ? AND region = ?",
            [o["year_month"], o["region"]],
        )
        con.execute(
            "INSERT INTO mart.fact_targets VALUES (?, ?, ?, 'override')",
            [o["year_month"], o["region"], o["target_brl"]],
        )
    return con.execute("SELECT count(*) FROM mart.fact_targets").fetchone()[0]


def _conditions(
    dataset: str, start: date | None, end: date | None, filters: dict[str, str]
) -> tuple[list[str], list]:
    clauses, params = [], []
    if start:
        clauses.append("date >= ?")
        params.append(start)
    if end:
        clauses.append("date <= ?")
        params.append(end)
    for name, value in filters.items():
        if name in COMMON_DIMS:
            clauses.append(f"{COMMON_DIMS[name]} = ?")
        elif name in LINE_DIMS and dataset == "v_sales":
            clauses.append(f"{LINE_DIMS[name]} = ?")
        elif name in LINE_DIMS:
            # An order belongs to a category or seller if one of its lines does.
            clauses.append(
                f"order_id IN (SELECT order_id FROM mart.v_sales WHERE {LINE_DIMS[name]} = ?)"
            )
        else:
            raise ValueError(f"unknown filter {name!r}")
        params.append(value)
    return clauses, params


def _where(clauses: list[str]) -> str:
    return (" WHERE " + " AND ".join(clauses)) if clauses else ""


def _group_column(dataset: str, by: str | None) -> str | None:
    if by is None:
        return None
    if by in COMMON_DIMS:
        return COMMON_DIMS[by]
    if by in LINE_DIMS and dataset == "v_sales":
        return LINE_DIMS[by]
    return ""  # not available at this grain


def _dataset_values(con, user, dataset, kpis, currency, start, end, filters, by) -> pd.DataFrame:
    group = _group_column(dataset, by)
    if group == "":
        return pd.DataFrame(columns=["key", *[k.id for k in kpis]])
    source, params = relation(con, user, dataset, aggregate=True)
    clauses, wparams = _conditions(dataset, start, end, filters)
    where = _where(clauses)
    cur = currency.lower()
    exprs = ", ".join(f"({k.expr.format(cur=cur)}) AS {k.id}" for k in kpis)
    key = f"{group} AS key, " if group else "NULL AS key, "
    sql = f"SELECT {key}{exprs} FROM ({source}){where}" + (
        f" GROUP BY {group} ORDER BY {group}" if group else ""
    )
    return con.execute(sql, params + wparams).df()


def _plan_attainment(con, user, start, end, filters, by) -> pd.DataFrame:
    """GMV on the days that have a plan ÷ the plan prorated to those days (in BRL)."""
    if (
        user.role == "seller"
        or any(f not in PLAN_DIMS for f in filters)
        or (by is not None and by not in PLAN_DIMS)
    ):
        return pd.DataFrame(columns=["key", "value"])
    source, params = relation(con, user, "v_sales", aggregate=True)
    clauses, wparams = _conditions("v_sales", start, end, filters)
    where = _where([*clauses, "NOT is_canceled"])
    # Plan days, restricted like the sales: same dates, same region filter, same user regions.
    day_clauses, day_params = ["TRUE"], []
    if start:
        day_clauses.append("d.date >= ?")
        day_params.append(start)
    if end:
        day_clauses.append("d.date <= ?")
        day_params.append(end)
    if "month" in filters:
        day_clauses.append("d.year_month = ?")
        day_params.append(filters["month"])
    if "region" in filters:
        day_clauses.append("t.region = ?")
        day_params.append(filters["region"])
    if user.role == "regional_manager":
        day_clauses.append(f"t.region IN ({', '.join('?' for _ in user.regions)})")
        day_params.extend(user.regions)
    key = {"month": "p.year_month", "region": "p.region", None: "NULL"}[by]
    sql = f"""
        WITH plan_days AS (
            SELECT d.date, d.year_month, t.region,
                   t.target_brl / day(last_day(d.date)) AS day_target
            FROM mart.dim_date d JOIN mart.fact_targets t ON t.year_month = d.year_month
            WHERE {" AND ".join(day_clauses)}
        ),
        sales AS (
            SELECT date, customer_region AS region, sum(price_brl) AS gmv
            FROM ({source}){where} GROUP BY ALL
        )
        SELECT {key} AS key,
               coalesce(sum(s.gmv), 0) / nullif(sum(p.day_target), 0) AS value
        FROM plan_days p
        LEFT JOIN sales s ON s.date = p.date AND s.region = p.region
        GROUP BY ALL
    """
    df = con.execute(sql, day_params + params + wparams).df()
    return df.dropna(subset=["value"]) if by else df


def compute(
    con: duckdb.DuckDBPyConnection,
    user: User,
    kpis: dict[str, Kpi],
    *,
    ids: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    filters: dict[str, str] | None = None,
    currency: str = "BRL",
    by: str | None = None,
) -> pd.DataFrame:
    """One row per KPI (and per group when ``by`` is given).

    Columns: key, kpi, name, group, unit, value, target, status. A KPI that cannot be computed
    for this cut (a plan for one seller, a category on reviews) has value NaN.
    """
    currency = currency.upper()
    if currency not in CURRENCIES:
        raise ValueError(f"currency must be one of {CURRENCIES}")
    if by is not None and by not in COMMON_DIMS and by not in LINE_DIMS:
        raise ValueError(f"cannot group by {by!r}")
    filters = filters or {}
    selected = [kpis[i] for i in ids] if ids else list(kpis.values())

    long = []
    for dataset in sorted({k.dataset for k in selected if k.dataset}):
        group = [k for k in selected if k.dataset == dataset]
        df = _dataset_values(con, user, dataset, group, currency, start, end, filters, by)
        for k in group:
            long.append(df[["key", k.id]].rename(columns={k.id: "value"}).assign(kpi=k.id))
    for k in selected:
        if k.plan_of:
            df = _plan_attainment(con, user, start, end, filters, by)
            long.append(df[["key", "value"]].assign(kpi=k.id))

    values = pd.concat(long, ignore_index=True) if long else pd.DataFrame()
    keys = (
        sorted(values["key"].drop_duplicates().tolist(), key=lambda v: (pd.isna(v), v))
        if by
        else [None]
    )
    rows = []
    for key in keys:
        for k in selected:
            hit = values[(values["kpi"] == k.id) & (values["key"].eq(key) if by else True)]
            value = (
                float(hit["value"].iloc[0])
                if len(hit) and pd.notna(hit["value"].iloc[0])
                else float("nan")
            )
            rows.append(
                {
                    "key": key,
                    "kpi": k.id,
                    "name": k.name,
                    "group": k.group,
                    "unit": k.unit,
                    "value": value,
                    "target": k.target,
                    "status": k.status(value),
                }
            )
    out = pd.DataFrame(rows)
    # pandas turns None into NaN; "no status" must stay None so callers can test for it.
    out["status"] = out["status"].astype(object).where(out["status"].notna(), None)
    return out if by else out.drop(columns="key")
