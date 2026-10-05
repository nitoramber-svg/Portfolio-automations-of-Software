"""The Quick Suite export: Parquet that reads back, Athena DDL that matches it, RLS rules."""

from __future__ import annotations

import json

import duckdb
import pytest

from bi_kpi import export
from bi_kpi.config import load_settings
from bi_kpi.security import load_users


@pytest.fixture
def exported(loaded, tmp_path):
    _, con = loaded
    users = load_users(load_settings().config_dir / "users.yaml")
    out = tmp_path / "export"
    rows = export.export(con, out, users, "s3://bucket/olist-bi/")
    return con, out, rows


def test_every_table_reads_back_with_its_rows(exported):
    con, out, rows = exported
    assert rows["fact_orders"] == con.execute("SELECT count(*) FROM mart.fact_orders").fetchone()[0]
    for name, n in rows.items():
        back = duckdb.sql(f"SELECT count(*) FROM '{(out / name / f'{name}.parquet').as_posix()}'")
        assert back.fetchone()[0] == n, name


def test_athena_ddl_points_each_table_at_its_folder(exported):
    _, out, rows = exported
    ddl = (out / "athena.sql").read_text(encoding="utf-8")
    assert ddl.count("CREATE EXTERNAL TABLE") == len(rows)
    assert "LOCATION 's3://bucket/olist-bi/v_sales/'" in ddl
    assert "`price_brl` decimal(12,2)" in ddl
    assert "`recipients` array<string>" in ddl  # alerts_log


def test_seller_datasets_carry_seller_id_and_no_order_totals(exported):
    _, out, _ = exported
    path = (out / "qs_orders_by_seller" / "qs_orders_by_seller.parquet").as_posix()
    cols = [r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM '{path}'").fetchall()]
    assert "seller_id" in cols
    assert "order_value_brl" not in cols and "payment_value_brl" not in cols


def test_manifest_flags_customer_columns(exported):
    _, out, _ = exported
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    by_table = {t["table"]: t for t in manifest["tables"]}
    assert by_table["v_sales"]["customer_columns"] == ["customer_city", "customer_unique_id"]
    assert by_table["fact_targets"]["customer_columns"] == []
    assert (out / "rls_rules.csv").read_text(encoding="utf-8").startswith("UserName,")


@pytest.mark.parametrize(
    ("duck", "athena"),
    [("DECIMAL(14,2)", "decimal(14,2)"), ("VARCHAR[]", "array<string>"), ("DATE", "date")],
)
def test_types(duck, athena):
    assert export.athena_type(duck) == athena


def test_unknown_types_fail_loudly():
    with pytest.raises(ValueError):
        export.athena_type("MAP(VARCHAR, INTEGER)")
