"""Row-level security: look for leaks, including an order split between two sellers."""

from __future__ import annotations

import duckdb
import pytest

from bi_kpi import kpis, pipeline, security
from bi_kpi.config import load_settings
from bi_kpi.olist import FILES
from bi_kpi.security import AccessDenied, User

CATALOG = kpis.load_kpis(load_settings().config_dir / "kpis.yaml")

DIRECTOR = User("direccion", "director")
ANALYST = User("analista", "analyst")
SUDESTE = User("gerente.sudeste", "regional_manager", regions=("Sudeste",))
S1 = User("vendedor.s1", "seller", seller_id="s1")
S2 = User("vendedor.s2", "seller", seller_id="s2")


@pytest.fixture
def con(settings):
    """The clean fixture plus o10: one order (Nordeste) with a line from s1 and one from s2."""
    rows = {
        "orders": "o10,c2,delivered,2017-03-08 10:00:00,2017-03-08 10:10:00,"
        "2017-03-09 09:00:00,2017-03-14 10:00:00,2017-03-25 00:00:00",
        "order_items": "o10,1,p1,s1,2017-03-10 10:00:00,40.00,4.00\n"
        "o10,2,p2,s2,2017-03-10 10:00:00,60.00,6.00",
        "order_payments": "o10,1,credit_card,1,110.00",
        "order_reviews": "r10,o10,2,,,2017-03-15 00:00:00,2017-03-15 08:00:00",
    }
    for table, text in rows.items():
        with (settings.raw_dir / FILES[table][0]).open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    pipeline.load(settings, offline_fx=True)
    c = duckdb.connect(str(settings.warehouse), read_only=True)
    yield c
    c.close()


def gmv(con, user):
    return kpis.compute(con, user, CATALOG, ids=["gmv"])["value"].iloc[0]


def test_a_seller_sees_only_their_own_lines_of_a_shared_order(con):
    lines = security.secure_rows(con, S1, "v_sales", ["order_id", "seller_id", "price_brl"])
    assert set(lines["seller_id"]) == {"s1"}
    assert lines[lines["order_id"] == "o10"]["price_brl"].tolist() == [40]
    assert gmv(con, S1) == 240  # o1 100 + o2 100 + its o10 line 40
    assert gmv(con, S2) == 170  # o4 30 + o5 80 + its o10 line 60


def test_a_seller_never_sees_order_totals_that_include_other_sellers(con):
    cols = security.visible_columns(con, S1, "v_orders")
    assert "order_value_brl" not in cols and "payment_value_brl" not in cols
    with pytest.raises(AccessDenied):
        security.secure_rows(con, S1, "v_orders", ["order_id", "order_value_brl"])
    # Not even through a hand-written aggregate: the column is not in the seller's relation.
    sql, params = security.relation(con, S1, "v_orders", aggregate=True)
    with pytest.raises(duckdb.BinderException):
        con.execute(f"SELECT sum(order_value_brl) FROM ({sql})", params)


def test_a_seller_sees_the_orders_and_reviews_their_lines_belong_to(con):
    orders = set(security.secure_rows(con, S1, "v_orders", ["order_id"])["order_id"])
    assert orders == {"o1", "o2", "o10"}
    reviews = set(security.secure_rows(con, S2, "v_reviews", ["order_id"])["order_id"])
    assert reviews == {"o5", "o10"}  # o3 and o4 were not reviewed


@pytest.mark.parametrize("dataset", security.DATASETS)
def test_a_regional_manager_gets_no_row_from_another_region(con, dataset):
    rows = security.secure_rows(con, SUDESTE, dataset, ["customer_region"])
    assert len(rows) > 0
    assert set(rows["customer_region"]) == {"Sudeste"}


def test_regional_kpis_match_the_director_filtered_by_region(con):
    mine = kpis.compute(con, SUDESTE, CATALOG).set_index("kpi")["value"]
    director = kpis.compute(con, DIRECTOR, CATALOG, filters={"region": "Sudeste"})
    director = director.set_index("kpi")["value"]
    assert mine.drop("plan_attainment").equals(director.drop("plan_attainment"))


def test_filtering_another_region_returns_nothing_not_its_data(con):
    v = kpis.compute(con, SUDESTE, CATALOG, ids=["orders"], filters={"region": "Nordeste"})
    assert v["value"].iloc[0] == 0


@pytest.mark.parametrize("dataset", security.DATASETS)
def test_the_analyst_sees_every_row_but_no_individual_customer(con, dataset):
    cols = security.visible_columns(con, ANALYST, dataset)
    assert not security.CUSTOMER_COLUMNS & set(cols)
    with pytest.raises(AccessDenied):
        security.secure_rows(con, ANALYST, dataset, ["customer_unique_id"])
    everything = len(security.secure_rows(con, DIRECTOR, dataset))
    assert len(security.secure_rows(con, ANALYST, dataset)) == everything


def test_the_analyst_still_gets_customer_counts(con):
    v = kpis.compute(con, ANALYST, CATALOG, ids=["unique_customers", "repeat_rate"])
    v = dict(zip(v["kpi"], v["value"], strict=True))
    assert v["unique_customers"] == 3  # u1 (o1, o4), u2 (o2, o10), u5
    assert v["repeat_rate"] == pytest.approx(2 / 3)


def test_unknown_users_are_refused():
    users = security.load_users(load_settings().config_dir / "users.yaml")
    with pytest.raises(AccessDenied):
        users.get("nadie")


def test_users_file_is_valid_and_exports_quick_suite_rules(tmp_path):
    users = security.load_users(load_settings().config_dir / "users.yaml")
    assert {u.role for u in users.by_name.values()} == set(security.ROLES)
    path = security.export_rls_rules(users, tmp_path / "rls.csv")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "UserName,customer_region,seller_id"
    assert "gerente.sudeste,Sudeste," in lines
    assert "direccion,," in lines
