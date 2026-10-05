"""Putting files together, who sees what, and the stories the demo must tell."""

from __future__ import annotations

import pandas as pd
import pytest

from mto_bi import dataset, metrics, roles
from mto_bi.schema import COSTS, PRODUCTION
from mto_bi.templates import workbook_bytes
from tests.conftest import AS_OF


def test_uploading_one_template_replaces_only_that_table(demo, demo_sample):
    fewer = demo_sample.production.head(100)
    updated = demo.with_files([("produccion_semana.xlsx", workbook_bytes(PRODUCTION, fewer))])
    assert len(updated.production) == 100
    assert len(updated.orders) == len(demo.orders) and len(updated.invoices) == len(demo.invoices)


def test_rows_pointing_at_unknown_orders_are_left_out_with_a_warning(demo):
    bad = pd.DataFrame(
        [{"folio_pedido": "P-NOPE", "partida": None, "concepto": "Flete", "importe": 10.0}]
    )
    updated = demo.with_files([("costos.xlsx", workbook_bytes(COSTS, bad))])
    assert updated.costs.empty
    assert any("P-NOPE" in i.message for i in updated.issues)


def test_the_same_row_in_two_files_counts_once(demo_sample):
    rows = demo_sample.production.head(10)
    d = dataset.build(
        dataset.classify(
            [
                ("p1.xlsx", workbook_bytes(PRODUCTION, rows)),
                ("p2.xlsx", workbook_bytes(PRODUCTION, rows)),
            ]
        )
    )
    assert any("otro archivo" in i.message for i in d.issues)


def test_nothing_loaded_is_a_valid_empty_dataset():
    d = dataset.empty()
    assert d.as_of is None and d.orders.empty and d.invoices.empty


# --- roles -------------------------------------------------------------------------------------


def every_column(d):
    return set().union(*(set(getattr(d, t).columns) for t in ("quotes", "orders", "production")))


def test_the_shop_sees_no_prices_and_no_costs(demo):
    d = roles.scope(demo, roles.BY_KEY["taller"])
    assert not every_column(d) & {"precio_venta", "importe", "folio_factura"}
    assert d.costs.empty and d.invoices.empty and d.quotes.empty
    assert len(d.production) == len(demo.production)


def test_sales_sees_no_costs_or_invoices(demo):
    d = roles.scope(demo, roles.BY_KEY["ventas"])
    assert d.costs.empty and d.invoices.empty
    assert len(d.quotes) == len(demo.quotes)


def test_a_seller_sees_only_their_own(demo):
    role = roles.BY_KEY["vendedor"]
    d = roles.scope(demo, role)
    assert set(d.quotes["vendedor"]) == {role.seller} and set(d.orders["vendedor"]) == {role.seller}
    assert set(d.production["folio_pedido"]) <= set(d.orders["folio_pedido"])
    assert 0 < len(d.orders) < len(demo.orders)


@pytest.mark.parametrize("role", roles.ROLES, ids=lambda r: r.key)
def test_every_role_can_only_upload_what_it_owns(role):
    if not role.sees_prices:
        assert set(role.uploads) <= {"produccion"}
    if not role.sees_costs:
        assert "costos" not in role.uploads


# --- the demo's stories, found by the same functions the pages use -----------------------------


def test_upholstery_is_the_slowest_stage(demo):
    s = metrics.stage_summary(demo.production, AS_OF)
    assert s.loc[s["mediana"].idxmax(), "etapa"] == "Tapicería"


def test_designers_close_more_than_private_clients(demo):
    by = metrics.close_rate_by(metrics.quote_folios(demo.quotes), "tipo_cliente").set_index(
        "tipo_cliente"
    )
    assert by.loc["Diseñador", "tasa"] > by.loc["Particular", "tasa"] + 0.1


def test_the_discounting_seller_closes_most_and_earns_least(demo):
    rates = metrics.close_rate_by(metrics.quote_folios(demo.quotes), "vendedor").set_index(
        "vendedor"
    )
    lines = metrics.line_margin(demo.orders, demo.costs).dropna(subset=["fecha_entrega"])
    margin = metrics.margin_by(lines, "vendedor").set_index("vendedor")["margen_pct"]
    assert rates["tasa"].idxmax() == "Diego Herrera" == margin.idxmin()


def test_atelier_lino_margin_falls_and_fabric_explains_it(demo):
    from mto_bi.app.main import Context
    from mto_bi.app.pages import margin_drop

    ctx = Context(
        roles.BY_KEY["direccion"],
        demo,
        pd.Timestamp("2025-10-04"),
        pd.Timestamp("2026-10-04"),
        AS_OF,
        True,
    )
    text = margin_drop(ctx)
    assert text and "Atelier Lino" in text and "tela" in text


def test_some_delivered_orders_are_not_invoiced(demo):
    matched, orphan = metrics.match_invoices(demo.orders, demo.invoices)
    assert len(metrics.to_invoice(matched, AS_OF)) > 0 and len(orphan) == 6
