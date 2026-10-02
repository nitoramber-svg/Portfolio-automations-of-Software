"""Every number the dashboard shows. Each function goes through the KPI engine or through
``security.relation()``, so row-level security applies to every chart; none reads a table
directly except the public reference ones (calendar, states) and, for the director and the
analyst only, the data quality report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import duckdb
import pandas as pd

from bi_kpi import kpis
from bi_kpi.kpis import Kpi
from bi_kpi.security import AccessDenied, User, relation

# Who may see the data quality report: it counts quarantined orders of every region.
QUALITY_ROLES = ("director", "analyst")

# Below this many orders a month or region is too thin to raise an alert.
MIN_ORDERS = 30


@dataclass(frozen=True)
class View:
    """What the sidebar selected."""

    user: User
    start: date | None = None
    end: date | None = None
    filters: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    currency: str = "BRL"

    @property
    def filter_dict(self) -> dict[str, str]:
        return dict(self.filters)


def _compute(con, view: View, catalog: dict[str, Kpi], ids=None, by=None) -> pd.DataFrame:
    return kpis.compute(
        con,
        view.user,
        catalog,
        ids=ids,
        start=view.start,
        end=view.end,
        filters=view.filter_dict,
        currency=view.currency,
        by=by,
    )


def kpi_values(con, view: View, catalog: dict[str, Kpi]) -> pd.DataFrame:
    """One row per KPI for the whole selection."""
    return _compute(con, view, catalog)


def kpi_by(con, view: View, catalog: dict[str, Kpi], ids: list[str], by: str) -> pd.DataFrame:
    """Wide table: one row per group, one column per KPI."""
    df = _compute(con, view, catalog, ids=ids, by=by)
    if df.empty:
        # Typed even when empty, so charts and nlargest() work for a user who sees nothing.
        return pd.DataFrame(
            {"key": pd.Series(dtype=object), **{i: pd.Series(dtype=float) for i in ids}}
        )
    return df.pivot(index="key", columns="kpi", values="value").reset_index()[["key", *ids]]


def plan_by_month(con, view: View) -> pd.DataFrame:
    return kpis.plan_vs_actual(
        con,
        view.user,
        start=view.start,
        end=view.end,
        filters=view.filter_dict,
        by="month",
        currency=view.currency,
    )


def state_map(con, view: View, catalog: dict[str, Kpi]) -> pd.DataFrame:
    """Sales and on-time delivery per customer state, with the state's center for the map."""
    df = kpi_by(con, view, catalog, ["gmv", "on_time_delivery", "orders"], "state")
    centers = con.execute("SELECT state, state_name, region, lat, lng FROM mart.dim_region").df()
    return centers.merge(df.rename(columns={"key": "state"}), on="state", how="inner")


def _secure(con, view: View, dataset: str) -> tuple[str, list]:
    """The user's rows of ``dataset`` with the sidebar filters applied."""
    source, params = relation(con, view.user, dataset, aggregate=True)
    clauses, wparams = kpis.conditions(dataset, view.start, view.end, view.filter_dict)
    return f"SELECT * FROM ({source}){kpis.where_sql(clauses)}", params + wparams


def installments(con, view: View) -> pd.DataFrame:
    """Orders by number of installments (non-canceled), grouped above 10."""
    sql, params = _secure(con, view, "v_orders")
    return con.execute(
        f"""
        SELECT CASE WHEN installments >= 10 THEN '10+' ELSE CAST(installments AS VARCHAR) END
                   AS installments,
               count(*) AS orders
        FROM ({sql}) WHERE NOT is_canceled AND installments IS NOT NULL
        GROUP BY 1 ORDER BY min(installments)
        """,
        params,
    ).df()


def delay_vs_score(con, view: View) -> pd.DataFrame:
    """Average review score by days late (0 = on time, capped at 15).

    Measured: the drop is a cliff, not a line (4.29 on time, ~3.7 one day late, ~1.7 from a
    week on), so a least-squares slope (-0.016 per day) would misread it; see delay_summary.
    """
    sql, params = _secure(con, view, "v_reviews")
    return con.execute(
        f"""
        SELECT least(days_late, 15) AS days_late, avg(score) AS score, count(*) AS reviews
        FROM ({sql}) WHERE days_late IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,
        params,
    ).df()


def delay_summary(by_day: pd.DataFrame) -> dict[str, float]:
    """Score on time, one day late, and a week or more late (weighted by reviews)."""

    def avg(rows: pd.DataFrame) -> float:
        n = rows["reviews"].sum()
        return float((rows["score"] * rows["reviews"]).sum() / n) if n else float("nan")

    return {
        "on_time": avg(by_day[by_day["days_late"] == 0]),
        "one_day": avg(by_day[by_day["days_late"] == 1]),
        "week_plus": avg(by_day[by_day["days_late"] >= 7]),
    }


def _complete_months(con) -> set[int]:
    return {
        ym
        for (ym,) in con.execute(
            "SELECT DISTINCT year_month FROM mart.dim_date WHERE is_complete_month"
        ).fetchall()
    }


def target_status(con, view: View, catalog: dict[str, Kpi], by: str) -> pd.DataFrame:
    """KPIs that have a target, per month or region, with status 'thin' when the group has
    fewer than MIN_ORDERS orders: one cancellation in six orders is not a 17 % problem.

    By month, only complete months: a half-month at the edge of the data is not an alert.
    """
    targeted = [k.id for k in catalog.values() if k.target is not None]
    df = _compute(con, view, catalog, ids=[*targeted, "orders"], by=by)
    volume = df[df["kpi"] == "orders"].set_index("key")["value"]
    df = df[df["kpi"] != "orders"].copy()
    if by == "month":
        df = df[df["key"].isin(_complete_months(con))]
    thin = df["key"].map(volume).fillna(0) < MIN_ORDERS
    df["status"] = df["status"].where(~thin, "thin")
    return df


def alerts(con, view: View, catalog: dict[str, Kpi]) -> pd.DataFrame:
    """Rule alerts: every (month, KPI) and (region, KPI) off its target in the selection.

    Anomaly detection against each series' own history comes in step 5.
    """
    out = pd.concat(
        [
            target_status(con, view, catalog, "month").assign(scope="Mes"),
            target_status(con, view, catalog, "region").assign(scope="Región"),
        ],
        ignore_index=True,
    )
    out = out[out["status"] == "off_target"].copy()
    out["gap"] = out["value"] - out["target"]
    return out[["scope", "key", "kpi", "name", "unit", "value", "target", "gap"]]


def options(con, view: View) -> dict[str, list[str]]:
    """Values the sidebar offers: regions the user may see, their states, categories."""
    regions = [
        r
        for (r,) in con.execute("SELECT DISTINCT region FROM mart.dim_region ORDER BY 1").fetchall()
    ]
    if view.user.role == "regional_manager":
        regions = [r for r in regions if r in view.user.regions]
    states = con.execute("SELECT region, state FROM mart.dim_region ORDER BY state").fetchall()
    categories = [
        c
        for (c,) in con.execute(
            "SELECT DISTINCT category FROM mart.dim_product WHERE category IS NOT NULL ORDER BY 1"
        ).fetchall()
    ]
    return {
        "regions": regions,
        "states": [s for r, s in states if r in regions],
        "states_by_region": {r: [s for rr, s in states if rr == r] for r in regions},
        "categories": categories,
    }


def date_bounds(con) -> tuple[date, date, date, date]:
    """(first purchase, last purchase, default start, default end).

    The default is the complete months, clipped to the purchases (the calendar runs on to the
    last estimated delivery); with no complete month, every purchase.
    """
    first, last, c0, c1 = con.execute(
        """
        SELECT (SELECT min(date) FROM mart.v_orders), (SELECT max(date) FROM mart.v_orders),
               min(date) FILTER (WHERE is_complete_month),
               max(date) FILTER (WHERE is_complete_month)
        FROM mart.dim_date
        """
    ).fetchone()
    if c0 is None:
        return first, last, first, last
    return first, last, max(c0, first), min(c1, last)


def _require_quality_role(user: User) -> None:
    if user.role not in QUALITY_ROLES:
        raise AccessDenied(f"{user.name} may not see the data quality report")


def quality_report(con, user: User) -> pd.DataFrame:
    _require_quality_role(user)
    return con.execute("SELECT * FROM quality.report").df()


def reconciliation(con, user: User) -> pd.DataFrame:
    _require_quality_role(user)
    return con.execute("SELECT * FROM quality.reconciliation").df()


def quarantine_rows(con, user: User) -> pd.DataFrame:
    """Quarantined orders with their reason (order id and dates only, no customer data)."""
    _require_quality_role(user)
    return con.execute(
        "SELECT order_id, order_status, purchased_at, delivered_at, reasons "
        "FROM quarantine.orders ORDER BY purchased_at"
    ).df()


def monthly_completeness(con, user: User) -> pd.DataFrame:
    """Orders per purchase month (all statuses) and whether the month is complete.

    Every month between the first and the last purchase, so a month without a single order
    (Nov 2016) shows up as the gap it is.
    """
    _require_quality_role(user)
    return con.execute(
        """
        SELECT d.year_month, any_value(d.is_complete_month) AS complete,
               count(o.order_id) AS orders
        FROM mart.dim_date d LEFT JOIN mart.v_orders o ON o.date = d.date
        WHERE d.date BETWEEN (SELECT min(date) FROM mart.v_orders)
                         AND (SELECT max(date) FROM mart.v_orders)
        GROUP BY 1 ORDER BY 1
        """
    ).df()


def connect(path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path), read_only=True)
