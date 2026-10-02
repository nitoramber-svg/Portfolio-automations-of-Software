"""Export for AWS / Amazon Quick Suite (QuickSight): Parquet + Athena DDL + RLS rules.

``bi export`` writes, under one folder that mirrors the S3 layout:

    <table>/<table>.parquet   every mart table, the three datasets and the sales plan
    athena.sql                CREATE EXTERNAL TABLE for each, pointing at s3://<bucket>/<table>/
    rls_rules.csv             the permissions dataset QuickSight applies (security.export_rls_rules)
    manifest.json             tables, row counts and what each is for

Two extra datasets exist only for QuickSight, whose row-level security can only match a column
of the dataset itself (it cannot do the "orders that contain one of my lines" this project's
security layer does): ``qs_orders_by_seller`` and ``qs_reviews_by_seller`` repeat each order /
review once per seller with lines in it, so a seller's rule ``seller_id = X`` works. Their totals
double-count the 1,278 orders shared by sellers: they are for sellers' views only. See
docs/quicksuite.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb

from bi_kpi.security import CUSTOMER_COLUMNS, ORDER_TOTAL_COLUMNS, Users, export_rls_rules

# (schema.table, purpose) — what goes to S3.
TABLES = {
    "mart.dim_date": "Calendario (con meses completos y feriados)",
    "mart.dim_region": "Estados y regiones de Brasil",
    "mart.dim_customer": "Clientes (contiene datos individuales: seguridad por columna)",
    "mart.dim_seller": "Vendedores",
    "mart.dim_product": "Productos y categorías en español",
    "mart.dim_payment_type": "Formas de pago",
    "mart.fact_orders": "Hecho: un pedido por fila",
    "mart.fact_order_items": "Hecho: una línea de pedido por fila",
    "mart.fact_reviews": "Hecho: una reseña por pedido",
    "mart.fact_targets": "Meta de ventas por región y mes",
    "mart.agg_sales_daily": "Agregado diario para tableros rápidos (SPICE)",
    "mart.v_sales": "Dataset QuickSight: líneas (RLS por región del cliente y vendedor)",
    "mart.v_orders": "Dataset QuickSight: pedidos (RLS por región del cliente)",
    "mart.v_reviews": "Dataset QuickSight: última reseña por pedido (RLS por región)",
    "alerts.log": "Alertas enviadas (paso 5)",
    "alerts.events": "Calendario de eventos",
}

_SELLER_SAFE_ORDERS = """
    SELECT o.* EXCLUDE ({hidden}), s.seller_id
    FROM mart.v_orders o
    JOIN (SELECT DISTINCT order_id, seller_id FROM mart.v_sales) s USING (order_id)
"""
_SELLER_SAFE_REVIEWS = """
    SELECT r.*, s.seller_id
    FROM mart.v_reviews r
    JOIN (SELECT DISTINCT order_id, seller_id FROM mart.v_sales) s USING (order_id)
"""
EXTRA = {
    "qs_orders_by_seller": (
        _SELLER_SAFE_ORDERS.format(hidden=", ".join(sorted(ORDER_TOTAL_COLUMNS))),
        "Solo para vendedores: un pedido por vendedor con líneas en él, sin totales del pedido",
    ),
    "qs_reviews_by_seller": (
        _SELLER_SAFE_REVIEWS,
        "Solo para vendedores: una reseña por vendedor con líneas en el pedido",
    ),
}

_ATHENA_TYPES = {
    "VARCHAR": "string",
    "INTEGER": "int",
    "BIGINT": "bigint",
    "HUGEINT": "decimal(38,0)",
    "SMALLINT": "smallint",
    "DOUBLE": "double",
    "FLOAT": "float",
    "BOOLEAN": "boolean",
    "DATE": "date",
    "TIMESTAMP": "timestamp",
    "TIMESTAMP_NS": "timestamp",
    "VARCHAR[]": "array<string>",
}


def athena_type(duck: str) -> str:
    m = re.fullmatch(r"DECIMAL\((\d+),\s*(\d+)\)", duck)
    if m:
        return f"decimal({m.group(1)},{m.group(2)})"
    try:
        return _ATHENA_TYPES[duck]
    except KeyError:
        raise ValueError(f"no Athena type for {duck}") from None


def _ddl(name: str, cols: list[tuple[str, str]], bucket: str) -> str:
    body = ",\n".join(f"  `{c}` {athena_type(t)}" for c, t in cols)
    return (
        f"CREATE EXTERNAL TABLE IF NOT EXISTS olist_bi.{name} (\n{body}\n)\n"
        f"STORED AS PARQUET\nLOCATION '{bucket.rstrip('/')}/{name}/';\n"
    )


def export(con: duckdb.DuckDBPyConnection, out: Path, users: Users, bucket: str) -> dict[str, int]:
    """Write the export folder; returns rows per table."""
    out.mkdir(parents=True, exist_ok=True)
    sources = {
        full.split(".")[1] if full.startswith("mart.") else full.replace(".", "_"): (
            f"SELECT * FROM {full}",
            purpose,
        )
        for full, purpose in TABLES.items()
    }
    sources.update(EXTRA)
    rows, ddl, manifest = {}, ["CREATE DATABASE IF NOT EXISTS olist_bi;\n"], []
    for name, (sql, purpose) in sources.items():
        folder = out / name
        folder.mkdir(exist_ok=True)
        path = folder / f"{name}.parquet"
        con.execute(f"COPY ({sql}) TO '{path.as_posix()}' (FORMAT PARQUET)")
        cols = [(r[0], r[1]) for r in con.execute(f"DESCRIBE {sql}").fetchall()]
        n = con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0]
        rows[name] = n
        ddl.append(_ddl(name, cols, bucket))
        manifest.append(
            {
                "table": name,
                "rows": n,
                "purpose": purpose,
                "customer_columns": sorted(c for c, _ in cols if c in CUSTOMER_COLUMNS),
            }
        )
    (out / "athena.sql").write_text("\n".join(ddl), encoding="utf-8")
    export_rls_rules(users, out / "rls_rules.csv")
    (out / "manifest.json").write_text(
        json.dumps({"bucket": bucket, "tables": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return rows
