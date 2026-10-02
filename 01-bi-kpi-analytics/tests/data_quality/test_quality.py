"""Quality checks on a fixture with one planted defect per check, so every count is known."""

from __future__ import annotations

import duckdb
import pytest

from bi_kpi import pipeline, quality
from bi_kpi.olist import FILES

# Rows appended to the clean fixture. Each comment says what the checks must do with it.
DIRTY = {
    "orders": [
        # delivered without a delivery date -> quarantine
        "o6,c2,delivered,2017-03-06 10:00:00,2017-03-06 10:10:00,2017-03-07 09:00:00,,"
        "2017-03-25 00:00:00",
        # canceled yet delivered -> quarantine
        "o7,c3,canceled,2017-03-06 11:00:00,2017-03-06 11:10:00,2017-03-07 09:00:00,"
        "2017-03-12 10:00:00,2017-03-25 00:00:00",
        # handed to the carrier 15 min before purchase -> warning, kept
        "o8,c5,delivered,2017-03-07 12:00:00,2017-03-07 12:30:00,2017-03-07 11:45:00,"
        "2017-03-14 10:00:00,2017-03-25 00:00:00",
        # delivered before it was bought -> quarantine
        "o9,c1,delivered,2017-03-07 13:00:00,2017-03-07 13:10:00,2017-03-08 09:00:00,"
        "2017-03-02 10:00:00,2017-03-25 00:00:00",
    ],
    "order_items": [
        "o6,1,p1,s1,2017-03-08 10:00:00,40.00,5.00",
        "o7,1,p2,s1,2017-03-08 10:00:00,60.00,0.00",
        "o8,1,p1,s3,2017-03-09 10:00:00,70.00,10.00",
        "o8,2,p1,s3,2017-03-09 10:00:00,-5.00,0.00",  # negative price -> quarantine
        "o9,1,p1,s1,2017-03-09 10:00:00,25.00,5.00",
        "o99,1,p1,s1,2017-03-09 10:00:00,15.00,1.00",  # no such order -> quarantine
    ],
    "order_payments": [
        "o6,1,credit_card,1,45.00",
        "o7,1,boleto,1,60.00",
        "o8,1,credit_card,4,88.00",  # 75 of items, paid in 4 installments -> interest (info)
        "o9,1,voucher,1,30.00",
    ],
    "order_reviews": [
        "r4,o5,2,,,2017-03-20 00:00:00,2017-03-20 10:00:00",  # o5 reviewed again (r3 first)
        "r5,o6,3,,,2017-03-20 00:00:00,2017-03-20 10:00:00",
    ],
    "sellers": [
        "s3,13023,campinas/sp,SP",  # cut
        "s4,80020,vendas@loja.com.br,PR",  # junk -> city of the zip
        "s5,13023,campinsa,SP",  # typo -> fuzzy
        "s6,13023,rio claro,PR",  # zip 13023 is in SP only -> state warning
    ],
}

GEOLOCATION = "\n".join(
    [
        ",".join(FILES["geolocation"][1]),
        "13023,-22.90,-47.06,campinas,SP",
        "13023,-22.91,-47.07,campinas,SP",
        "80020,-25.43,-49.27,curitiba,PR",
        "80010,-25.43,-49.27,curitiba,PR",
        "1310,-23.56,-46.65,são paulo,SP",
        "40010,-12.97,-38.51,salvador,BA",
        "20040,-22.90,-43.17,rio de janeiro,RJ",
        "",
    ]
)


@pytest.fixture
def dirty(settings):
    for table, rows in DIRTY.items():
        with (settings.raw_dir / FILES[table][0]).open("a", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")
    (settings.raw_dir / FILES["geolocation"][0]).write_text(GEOLOCATION, encoding="utf-8")
    report = pipeline.load(settings, offline_fx=True)
    con = duckdb.connect(str(settings.warehouse), read_only=True)
    yield report, con
    con.close()


def rows(con, sql, *params):
    return con.execute(sql, list(params)).fetchall()


def report_counts(con):
    return dict(rows(con, "SELECT check_id, rows FROM quality.report"))


def test_contradictory_orders_are_quarantined_with_their_reason(dirty):
    _, con = dirty
    assert dict(rows(con, "SELECT order_id, reasons FROM quarantine.orders")) == {
        "o6": "orders_delivered_without_date",
        "o7": "orders_canceled_but_delivered",
        "o9": "orders_before_purchase",
    }
    kept = {r[0] for r in rows(con, "SELECT order_id FROM mart.fact_orders")}
    assert kept == {"o1", "o2", "o3", "o4", "o5", "o8"}


def test_children_follow_their_quarantined_order(dirty):
    report, con = dirty
    items = dict(
        rows(con, "SELECT order_id || '#' || item_seq, reasons FROM quarantine.order_items")
    )
    assert items == {
        "o6#1": "parent_order_quarantined",
        "o7#1": "parent_order_quarantined",
        "o9#1": "parent_order_quarantined",
        "o8#2": "items_invalid_amount",
        "o99#1": "items_orphan",
    }
    assert report.quarantined["order_payments"] == 3
    assert rows(con, "SELECT review_id FROM quarantine.order_reviews") == [("r5",)]


def test_warnings_stay_in_the_marts(dirty):
    _, con = dirty
    counts = report_counts(con)
    assert counts["orders_shipped_before_purchase"] == 1
    assert counts["orders_shipped_before_approval"] == 1
    # o8 keeps its valid line only: 70 + 10.
    assert rows(con, "SELECT order_value_brl FROM mart.fact_orders WHERE order_id = 'o8'") == [
        (80,)
    ]


def test_marts_reconcile_with_staging(dirty):
    _, con = dirty
    assert all(ok for (ok,) in rows(con, "SELECT ok FROM quality.reconciliation"))
    # Clean fixture 510 + o8 70 kept; o6 40, o7 60, o9 25, o99 15 and o8's -5 quarantined.
    assert rows(con, "SELECT sum(price_brl) FROM mart.fact_order_items") == [(580,)]
    assert rows(con, "SELECT sum(price_brl) FROM quarantine.order_items") == [(135,)]


def test_reconciliation_refuses_marts_that_lost_rows(dirty, settings):
    _, con = dirty
    con.close()
    with duckdb.connect(str(settings.warehouse)) as rw:
        rw.execute("DELETE FROM mart.fact_order_items WHERE order_id = 'o1'")
        with pytest.raises(quality.ReconciliationError, match="price BRL"):
            quality.reconcile(rw)


def test_payment_differences_are_split_by_cause(dirty):
    _, con = dirty
    counts = report_counts(con)
    assert counts["orders_installment_interest"] == 1  # o8
    assert counts["orders_payment_mismatch"] == 0


def test_report_lists_every_check_even_when_clean(dirty):
    _, con = dirty
    counts = report_counts(con)
    assert set(counts) == {c.id for c in quality.CHECKS}
    assert counts["orders_duplicated"] == 1  # o1 twice in the clean fixture: fixed
    assert counts["orders_incomplete_month"] == 0
    assert counts["customers_unknown_state"] == 0


def test_report_puts_a_price_on_quarantined_orders(dirty):
    _, con = dirty
    value = rows(
        con,
        "SELECT orders, value_brl FROM quality.report "
        "WHERE check_id = 'orders_delivered_without_date'",
    )
    assert value == [(1, 45)]  # o6: 40 + 5


def test_seller_cities_are_repaired(dirty):
    _, con = dirty
    cities = dict(rows(con, "SELECT seller_id, city FROM mart.dim_seller"))
    assert cities["s3"] == "campinas"  # "campinas/sp"
    assert cities["s4"] == "curitiba"  # an e-mail address, replaced by its zip's city
    assert cities["s5"] == "campinas"  # "campinsa"
    assert cities["s6"] == "rio claro"  # not a typo of anything: left as typed
    counts = report_counts(con)
    assert counts["sellers_city_fixed"] == 3
    assert counts["sellers_state_differs_from_zip"] == 1
    assert counts["customers_city_fixed"] == 0


def test_latest_review_per_order(dirty):
    _, con = dirty
    latest = rows(
        con, "SELECT review_id FROM mart.fact_reviews WHERE order_id = 'o5' AND is_latest_for_order"
    )
    assert latest == [("r4",)]
    assert report_counts(con)["reviews_several_per_order"] == 2


def test_check_definitions_are_consistent():
    ids = [c.id for c in quality.CHECKS]
    assert len(ids) == len(set(ids))
    assert {c.severity for c in quality.CHECKS} <= set(quality.SEVERITIES)
    assert {c.table for c in quality.CHECKS} <= set(quality.KEYS) | {"calendar"}


def test_incomplete_months_are_the_thin_edges_and_the_gaps():
    check = next(c for c in quality.CHECKS if c.id == "orders_incomplete_month")
    con = duckdb.connect()
    con.execute("CREATE SCHEMA stg")
    con.execute(
        "CREATE TABLE stg.orders AS "
        "SELECT TIMESTAMP '2017-01-10' AS purchased_at FROM range(3) "  # thin start
        "UNION ALL SELECT TIMESTAMP '2017-02-10' FROM range(100) "
        "UNION ALL SELECT TIMESTAMP '2017-04-10' FROM range(100) "  # March: a gap
        "UNION ALL SELECT TIMESTAMP '2017-05-10' FROM range(100) "
        # cut-off: the gap and thin edges pull the median to (9 + 100) / 2, threshold 5.45
        "UNION ALL SELECT TIMESTAMP '2017-06-10' FROM range(5)"
    )
    flagged = sorted(k for k, _ in con.execute(check.sql).fetchall())
    assert flagged == ["2017-01", "2017-03", "2017-06"]
    assert con.execute(check.total()).fetchone()[0] == 6
