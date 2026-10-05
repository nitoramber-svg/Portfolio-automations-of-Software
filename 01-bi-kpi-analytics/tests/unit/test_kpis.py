"""KPI engine on the hand-made fixture: every expected value is worked out in the comments.

Fixture (March 2017): o1 c1/u1 SP delivered on time, 100 + 10 freight, seller s1
                      o2 c2/u2 BA delivered 3 days late, 2 x (50 + 5), s1
                      o3 c3/u3 RJ canceled, 200 + 20, s2
                      o4 c4/u1 SP shipped, not delivered, 30 + 10, s2
                      o5 c5/u5 PR delivered on time, 80 + 20, s2
Reviews (latest per order): o1 5, o2 1, o5 4.
"""

from __future__ import annotations

import math
from datetime import date

import duckdb
import pytest

from bi_kpi import kpis
from bi_kpi.config import load_settings
from bi_kpi.security import User

CATALOG = kpis.load_kpis(load_settings().config_dir / "kpis.yaml")
DIRECTOR = User("direccion", "director")


def values(con, user=DIRECTOR, **kw):
    df = kpis.compute(con, user, CATALOG, **kw)
    return dict(zip(df["kpi"], df["value"], strict=True))


def test_every_kpi_on_the_fixture(loaded):
    _, con = loaded
    v = values(con)
    assert v["gmv"] == pytest.approx(310)  # 100 + 100 + 30 + 80; o3 canceled
    assert v["orders"] == 4
    assert v["avg_ticket"] == pytest.approx(77.5)  # 310 / 4
    assert v["unique_customers"] == 3  # u1 (o1, o4), u2, u5
    assert v["repeat_rate"] == pytest.approx(1 / 3)  # u1
    assert v["on_time_delivery"] == pytest.approx(2 / 3)  # o1, o5 of o1, o2, o5
    assert v["delivery_days"] == pytest.approx((7 + 22 + 10) / 3)
    assert v["freight_share"] == pytest.approx(50 / 310)
    assert v["cancel_rate"] == pytest.approx(1 / 5)
    assert v["avg_score"] == pytest.approx(10 / 3)
    assert v["negative_reviews"] == pytest.approx(1 / 3)
    assert v["active_sellers"] == 2
    assert math.isnan(v["plan_attainment"])  # no plan for March 2017


def test_status_follows_direction_and_target(loaded):
    _, con = loaded
    df = kpis.compute(con, DIRECTOR, CATALOG).set_index("kpi")
    assert df.loc["on_time_delivery", "status"] == "off_target"  # 66.7 % < 90 %
    assert df.loc["freight_share", "status"] == "ok"  # 16.1 % <= 20 %
    assert df.loc["cancel_rate", "status"] == "off_target"  # 20 % > 2 %
    assert df.loc["gmv", "status"] is None  # no target


def test_currency_conversion(loaded):
    _, con = loaded
    v = values(con, currency="MXN", ids=["gmv", "freight_share"])
    assert v["gmv"] == pytest.approx(310 * 5.7)  # fallback rate in the fixture
    assert v["freight_share"] == pytest.approx(50 / 310)  # a ratio does not change


def test_filters_and_grouping(loaded):
    _, con = loaded
    assert values(con, filters={"region": "Sudeste"}, ids=["gmv"])["gmv"] == 130  # o1 + o4
    assert values(con, end=date(2017, 3, 1), ids=["orders"])["orders"] == 2  # o1, o2
    df = kpis.compute(con, DIRECTOR, CATALOG, ids=["gmv"], by="region")
    assert dict(zip(df["key"], df["value"], strict=True)) == {
        "Nordeste": 100,
        "Sudeste": 130,
        "Sul": 80,
    }


def test_line_filters_reach_orders_and_reviews_through_their_lines(loaded):
    _, con = loaded
    v = values(con, filters={"seller": "s1"}, ids=["orders", "avg_score", "gmv"])
    assert v["orders"] == 2  # o1, o2
    assert v["avg_score"] == pytest.approx(3)  # 5 and 1
    assert v["gmv"] == 200


def test_a_kpi_without_that_grain_is_empty_not_wrong(loaded):
    _, con = loaded
    df = kpis.compute(con, DIRECTOR, CATALOG, ids=["avg_score", "gmv"], by="category")
    assert df[df["kpi"] == "avg_score"]["value"].isna().all()
    assert df[df["kpi"] == "gmv"]["value"].notna().all()


@pytest.fixture
def with_plan(loaded, settings):
    """March 2017 plan of 3,100 for Sudeste: 100 a day."""
    _, con = loaded
    con.close()
    with duckdb.connect(str(settings.warehouse)) as rw:
        rw.execute("INSERT INTO mart.fact_targets VALUES (201703, 'Sudeste', 3100, 'override')")
    con = duckdb.connect(str(settings.warehouse), read_only=True)
    yield con
    con.close()


def test_plan_is_prorated_by_day_and_counts_only_planned_sales(with_plan):
    v = values(with_plan, start=date(2017, 3, 1), end=date(2017, 3, 10), ids=["plan_attainment"])
    # 10 plan days = 1,000; Sudeste sold o1 100 + o4 30. o2 (Nordeste) has no plan: excluded.
    assert v["plan_attainment"] == pytest.approx(0.13)


def test_no_plan_for_cuts_the_plan_does_not_have(with_plan):
    seller = User("vendedor.s1", "seller", seller_id="s1")
    assert math.isnan(values(with_plan, user=seller, ids=["plan_attainment"])["plan_attainment"])
    by_cat = values(with_plan, filters={"category": "PC gamer"}, ids=["plan_attainment"])
    assert math.isnan(by_cat["plan_attainment"])


def _plan_inputs(con, monthly_gmv: dict[int, float], incomplete: tuple[int, ...] = ()):
    con.execute("CREATE SCHEMA mart")
    con.execute(
        "CREATE TABLE mart.dim_date AS SELECT CAST(d AS DATE) AS date, "
        "CAST(strftime(d, '%Y%m') AS INTEGER) AS year_month, TRUE AS is_complete_month "
        "FROM generate_series(TIMESTAMP '2017-01-01', TIMESTAMP '2017-06-30', INTERVAL 1 DAY) t(d)"
    )
    for ym in incomplete:
        con.execute("UPDATE mart.dim_date SET is_complete_month = FALSE WHERE year_month = ?", [ym])
    con.execute("CREATE TABLE mart.dim_region AS SELECT 'Sul' AS region UNION SELECT 'Norte'")
    con.execute(
        "CREATE TABLE mart.v_sales (year_month INTEGER, customer_region VARCHAR, "
        "price_brl DECIMAL(12, 2), is_canceled BOOLEAN)"
    )
    for ym, gmv in monthly_gmv.items():
        con.execute("INSERT INTO mart.v_sales VALUES (?, 'Sul', ?, FALSE)", [ym, gmv])
    con.execute("INSERT INTO mart.v_sales VALUES (201704, 'Sul', 9999, TRUE)")  # canceled


def test_run_rate_targets():
    con = duckdb.connect()
    _plan_inputs(con, {201701: 100, 201702: 200, 201703: 300, 201704: 400})
    kpis.build_targets(con, kpis.TargetSettings(monthly_growth=0.05, lookback_months=3))
    t = {
        (ym, r): float(v)
        for ym, r, v in con.execute(
            "SELECT year_month, region, target_brl FROM mart.fact_targets"
        ).fetchall()
    }
    assert t[(201704, "Sul")] == pytest.approx(210)  # (100 + 200 + 300) / 3 * 1.05
    assert t[(201705, "Sul")] == pytest.approx(315)  # (200 + 300 + 400) / 3 * 1.05
    assert (201703, "Sul") not in t  # fewer than 3 months behind it
    assert t[(201704, "Norte")] == 0  # a region with no sales gets a zero plan, not a gap


def test_no_target_on_or_after_an_incomplete_month():
    con = duckdb.connect()
    _plan_inputs(con, {201701: 100, 201702: 200, 201703: 300}, incomplete=(201702,))
    kpis.build_targets(con, kpis.TargetSettings(monthly_growth=0.05, lookback_months=3))
    months = {ym for (ym,) in con.execute("SELECT year_month FROM mart.fact_targets").fetchall()}
    assert months == {201706}  # Mar, Apr, May all look back over February


def test_overrides_win():
    con = duckdb.connect()
    _plan_inputs(con, {201701: 100, 201702: 200, 201703: 300})
    settings = kpis.TargetSettings(
        monthly_growth=0.05,
        lookback_months=3,
        overrides=({"year_month": 201704, "region": "Sul", "target_brl": 1000},),
    )
    kpis.build_targets(con, settings)
    assert con.execute(
        "SELECT target_brl, method FROM mart.fact_targets "
        "WHERE year_month = 201704 AND region = 'Sul'"
    ).fetchall() == [(1000, "override")]


def test_catalog_is_valid():
    assert {k.dataset for k in CATALOG.values() if k.dataset} <= {
        "v_sales",
        "v_orders",
        "v_reviews",
    }
    assert CATALOG["plan_attainment"].plan_of == "gmv"
