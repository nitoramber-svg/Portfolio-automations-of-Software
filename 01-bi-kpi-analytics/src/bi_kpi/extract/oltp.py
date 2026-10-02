"""SQL source: a SQLite database that plays the company's transactional system.

Olist ships orders and payments as CSVs, so ``seed_oltp`` first loads them into SQLite;
``extract_oltp`` then pulls them the way a BI pipeline pulls from a production database:
with SQL, incrementally, using the purchase timestamp as the watermark.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import duckdb
import pandas as pd

from bi_kpi.olist import FILES, OLTP_TABLES

# Column used as the incremental watermark for each table.
WATERMARKS = {"orders": "order_purchase_timestamp"}


def seed_oltp(raw_dir: Path, db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    with sqlite3.connect(db_path) as db:
        for table in OLTP_TABLES:
            file_name, _ = FILES[table]
            with (raw_dir / file_name).open(encoding="utf-8-sig", newline="") as fh:
                reader = csv.reader(fh)
                header = next(reader)
                cols = ", ".join(f'"{c}" TEXT' for c in header)
                db.execute(f"CREATE TABLE {table} ({cols})")
                marks = ", ".join("?" for _ in header)
                db.executemany(f"INSERT INTO {table} VALUES ({marks})", reader)
        db.execute("CREATE INDEX ix_orders_purchase ON orders (order_purchase_timestamp)")
        db.execute("CREATE INDEX ix_payments_order ON order_payments (order_id)")


def extract_oltp(
    con: duckdb.DuckDBPyConnection, db_path: Path, since: str | None = None
) -> dict[str, int]:
    """Copy orders (and their payments) purchased on/after ``since`` into ``raw``."""
    queries = {
        "orders": "SELECT * FROM orders WHERE (? IS NULL OR order_purchase_timestamp >= ?)",
        "order_payments": (
            # IN, not JOIN: a duplicated order row must not duplicate its payments.
            "SELECT * FROM order_payments WHERE order_id IN ("
            "SELECT order_id FROM orders WHERE (? IS NULL OR order_purchase_timestamp >= ?))"
        ),
    }
    counts: dict[str, int] = {}
    with sqlite3.connect(db_path) as db:
        for table, sql in queries.items():
            df = pd.read_sql_query(sql, db, params=(since, since), dtype=str)
            con.register("_extract", df)
            con.execute(f"CREATE OR REPLACE TABLE raw.{table} AS SELECT * FROM _extract")
            con.unregister("_extract")
            counts[table] = len(df)
    return counts
