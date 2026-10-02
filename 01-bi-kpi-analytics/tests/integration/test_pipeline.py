"""Pipeline on the hand-made fixture, where every expected number is known in advance."""

from __future__ import annotations

import pytest

from bi_kpi import pipeline


def scalar(con, sql, *params):
    return con.execute(sql, list(params)).fetchone()[0]


def test_extracts_all_three_source_types(loaded):
    report, _ = loaded
    assert report.extracted["orders"] == 6  # SQL source, before dedup
    assert report.extracted["order_payments"] == 6
    assert report.extracted["products"] == 3  # flat file
    assert report.extracted["fx_rates"] == 2  # API (fallback here)
    assert "geolocation" not in report.extracted  # optional and absent
    assert report.fx_source == "fallback"


def test_duplicate_orders_are_removed(loaded):
    _, con = loaded
    assert scalar(con, "SELECT count(*) FROM mart.fact_orders") == 5


@pytest.mark.parametrize(
    ("order_id", "on_time", "days_late", "delivery_days"),
    [("o1", True, 0, 7), ("o2", False, 3, 22), ("o4", None, None, None)],
)
def test_delivery_performance(loaded, order_id, on_time, days_late, delivery_days):
    _, con = loaded
    row = con.execute(
        "SELECT on_time, days_late, delivery_days FROM mart.fact_orders WHERE order_id = ?",
        [order_id],
    ).fetchone()
    assert row == (on_time, days_late, delivery_days)


def test_order_value_and_payments(loaded):
    _, con = loaded
    row = con.execute(
        "SELECT o.items, o.order_value_brl, o.payment_value_brl, o.installments, pt.payment_type "
        "FROM mart.fact_orders o JOIN mart.dim_payment_type pt USING (payment_type_key) "
        "WHERE order_id = 'o5'"
    ).fetchone()
    # voucher 20 + card 80: the main payment type is the larger one.
    assert row == (1, 100, 100, 2, "credit_card")


def test_canceled_orders_are_excluded_from_sales(loaded):
    _, con = loaded
    assert scalar(con, "SELECT is_canceled FROM mart.fact_orders WHERE order_id = 'o3'") is True
    # o1 100 + o2 50 + 50 + o4 30 + o5 80; o3 (200) is canceled.
    assert scalar(con, "SELECT sum(gmv_brl) FROM mart.agg_sales_daily") == 310


def test_no_sales_lost_between_staging_and_marts(loaded):
    _, con = loaded
    staged = scalar(con, "SELECT sum(price_brl) FROM stg.order_items")
    modeled = scalar(con, "SELECT sum(price_brl) FROM mart.fact_order_items")
    assert staged == modeled == 510


def test_currency_conversion_uses_fallback_rates(loaded):
    _, con = loaded
    row = con.execute(
        "SELECT price_mxn, price_usd, fx_source FROM mart.fact_order_items WHERE order_id = 'o1'"
    ).fetchone()
    assert row == (pytest.approx(570.0), pytest.approx(30.0), "fallback")


def test_customer_cleaning_and_regions(loaded):
    _, con = loaded
    rows = dict(
        con.execute(
            "SELECT customer_id, city || '/' || state || '/' || region FROM mart.dim_customer"
        ).fetchall()
    )
    assert rows["c1"] == "sao paulo/SP/Sudeste"  # accent and case normalized
    assert rows["c2"] == "salvador/BA/Nordeste"
    assert rows["c5"] == "curitiba/PR/Sul"  # lowercase state fixed
    # c1 and c4 are the same person ordering twice.
    assert scalar(con, "SELECT count(DISTINCT customer_unique_id) FROM mart.dim_customer") == 4


def test_category_labels(loaded):
    _, con = loaded
    labels = dict(con.execute("SELECT product_id, category FROM mart.dim_product").fetchall())
    assert labels == {"p1": "Belleza y salud", "p2": "PC gamer", "p3": "Sin categoría"}
    # The translation CSV starts with a UTF-8 BOM; its English label must still join.
    assert (
        scalar(con, "SELECT category_en FROM mart.dim_product WHERE product_id = 'p1'")
        == "health_beauty"
    )


def test_reviews(loaded):
    _, con = loaded
    assert scalar(con, "SELECT count(*) FROM mart.fact_reviews WHERE is_negative") == 1
    assert scalar(con, "SELECT has_comment FROM mart.fact_reviews WHERE review_id = 'r2'") is True
    assert scalar(con, "SELECT answer_hours FROM mart.fact_reviews WHERE review_id = 'r1'") == 36


def test_calendar_covers_estimated_deliveries(loaded):
    _, con = loaded
    d0, d1 = con.execute("SELECT min(date), max(date) FROM mart.dim_date").fetchone()
    assert str(d0) == "2017-03-01"
    assert str(d1) == "2017-03-30"


def test_incremental_extract_since(settings):
    report = pipeline.load(settings, offline_fx=True, since="2017-03-04")
    assert report.extracted["orders"] == 2  # o4, o5
    assert report.extracted["order_payments"] == 3


def test_reload_is_idempotent(settings):
    first = pipeline.load(settings, offline_fx=True)
    second = pipeline.load(settings, offline_fx=True)
    assert first.marts == second.marts
