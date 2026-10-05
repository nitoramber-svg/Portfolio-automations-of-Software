"""Dashboard speed, before/after the materialized datasets, and under 5x the real volume.

    python scripts/benchmark.py                 # pages per role, on the loaded warehouse
    python scripts/benchmark.py --compare-views # same, with the datasets as views (before)
    python scripts/benchmark.py --volume 5      # synthetic data at 5x Olist's volume: load + pages

Times are the data queries behind each page, cold (no Streamlit cache), median of 3 runs. They
are what docs/benchmark.md reports.
"""

from __future__ import annotations

import argparse
import os
import shutil
import statistics
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bi_kpi import kpis, pipeline, sample, security  # noqa: E402
from bi_kpi.config import load_settings  # noqa: E402
from bi_kpi.dashboard import data  # noqa: E402

# Olist: ~99k orders over Jan 2017 – Aug 2018 ≈ 163 a day. The sample generator ramps from 35 %
# to 100 % of its base, so a base of 240 a day reproduces the real volume.
REAL_BASE_PER_DAY = 240
USERS = [
    security.User("direccion", "director"),
    security.User("gerente.nordeste", "regional_manager", regions=("Nordeste",)),
]
DATASETS = ("v_sales", "v_orders", "v_reviews")


def pages(con, view, cat):
    return {
        "Resumen": lambda: (
            data.kpi_values(con, view, cat),
            data.kpi_by(
                con, view, cat, ["gmv", "orders", "on_time_delivery", "avg_score"], "month"
            ),
            data.plan_by_month(con, view),
            data.kpi_by(con, view, cat, ["gmv", "on_time_delivery", "plan_attainment"], "region"),
            data.alerts(con, view, cat),
        ),
        "Ventas": lambda: (
            data.kpi_by(con, view, cat, ["gmv", "orders", "avg_ticket"], "month"),
            data.state_map(con, view, cat),
            data.kpi_by(con, view, cat, ["gmv", "avg_ticket", "freight_share"], "category"),
            data.kpi_by(con, view, cat, ["gmv", "avg_ticket", "freight_share"], "seller"),
            data.installments(con, view),
        ),
        "Operación": lambda: (
            data.kpi_values(con, view, cat),
            data.kpi_by(
                con,
                view,
                cat,
                [
                    "on_time_delivery",
                    "delivery_days",
                    "avg_score",
                    "negative_reviews",
                    "cancel_rate",
                    "freight_share",
                ],
                "month",
            ),
            data.delay_vs_score(con, view),
        ),
    }


def measure(warehouse: Path, start: date, end: date) -> dict[tuple[str, str], float]:
    cat = kpis.load_kpis(load_settings().config_dir / "kpis.yaml")
    con = duckdb.connect(str(warehouse), read_only=True)
    top_seller = con.execute(
        "SELECT seller_id FROM mart.v_sales GROUP BY 1 ORDER BY sum(price_brl) DESC LIMIT 1"
    ).fetchone()[0]
    users = [*USERS[:2], security.User("vendedor", "seller", seller_id=top_seller)]
    out = {}
    for user in users:
        view = data.View(user, start, end)
        for name, fn in pages(con, view, cat).items():
            runs = []
            for _ in range(3):
                t = time.perf_counter()
                fn()
                runs.append(time.perf_counter() - t)
            out[(user.role, name)] = statistics.median(runs)
    con.close()
    return out


def as_views(warehouse: Path, into: Path) -> Path:
    """A copy of the warehouse with the datasets as views again (the "before")."""
    copy = into / "views.duckdb"
    shutil.copy(warehouse, copy)
    sql = (ROOT / "sql" / "marts" / "40_datasets.sql").read_text(encoding="utf-8")
    with duckdb.connect(str(copy)) as con:
        for ds in reversed(DATASETS):
            con.execute(f"DROP TABLE mart.{ds}")
        con.execute(sql.replace("CREATE OR REPLACE TABLE", "CREATE OR REPLACE VIEW"))
    return copy


def table(results: dict[str, dict], title: str) -> None:
    cols = list(results)
    print(f"\n{title}\n")
    print("| Rol | Página | " + " | ".join(cols) + " |")
    print("|---|---|" + "---:|" * len(cols))
    for role, page in next(iter(results.values())):
        cells = " | ".join(f"{results[c][(role, page)]:.2f} s" for c in cols)
        print(f"| {role} | {page} | {cells} |")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare-views", action="store_true")
    ap.add_argument("--volume", type=float, help="synthetic data at this multiple of Olist")
    args = ap.parse_args()
    start, end = date(2017, 1, 1), date(2018, 8, 31)

    if args.volume:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["BI_DATA_DIR"] = str(Path(tmp) / "data")
            settings = load_settings()
            t = time.perf_counter()
            counts = sample.generate(
                settings.raw_dir, orders_per_day=REAL_BASE_PER_DAY * args.volume
            )
            gen = time.perf_counter() - t
            t = time.perf_counter()
            pipeline.load(settings, offline_fx=True)
            load = time.perf_counter() - t
            print(
                f"Volume x{args.volume:g}: {counts['orders']:,} orders, "
                f"{counts['order_items']:,} lines"
            )
            print(f"  generate {gen:.1f} s, bi load {load:.1f} s")
            table(
                {f"x{args.volume:g}": measure(settings.warehouse, start, end)},
                f"Pages at x{args.volume:g} volume",
            )
            del os.environ["BI_DATA_DIR"]
        return 0

    warehouse = load_settings().warehouse
    results = {"Tablas (ahora)": measure(warehouse, start, end)}
    if args.compare_views:
        with tempfile.TemporaryDirectory() as tmp:
            results = {
                "Vistas (antes)": measure(as_views(warehouse, Path(tmp)), start, end),
                **results,
            }
    table(results, f"Pages, {start} → {end}, median of 3 cold runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
