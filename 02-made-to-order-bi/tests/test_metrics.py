"""The numbers on hand-made tables, where every answer is known."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mto_bi import metrics as m
from tests.conftest import ts


def quotes(rows):
    cols = [
        "folio",
        "partida",
        "fecha",
        "cliente",
        "tipo_cliente",
        "vendedor",
        "marca",
        "pieza",
        "cantidad",
        "importe",
        "estado",
        "fecha_cierre",
        "motivo_perdida",
    ]
    df = pd.DataFrame(rows, columns=cols)
    for c in ("fecha", "fecha_cierre"):
        df[c] = pd.to_datetime(df[c])
    return df


def q(
    folio,
    line,
    estado,
    importe,
    fecha="2026-03-01",
    cierre="2026-03-11",
    tipo="Diseñador",
    marca="Bruma",
    motivo=None,
):
    return [
        folio,
        line,
        fecha,
        "c",
        tipo,
        "Ana",
        marca,
        "p",
        1,
        importe,
        estado,
        None if estado == "Abierta" else cierre,
        motivo,
    ]


def test_a_quote_is_won_if_any_piece_is_and_open_while_undecided():
    f = m.quote_folios(
        quotes(
            [
                q("C1", 1, "Ganada", 100),
                q("C1", 2, "Perdida", 300),
                q("C2", 1, "Abierta", 50),
                q("C2", 2, "Perdida", 50),
                q("C3", 1, "Perdida", 80),
            ]
        )
    ).set_index("folio")
    assert f["estado"].to_dict() == {"C1": "Ganada", "C2": "Abierta", "C3": "Perdida"}
    assert f.loc["C1", "importe_ganado"] == 100 and f.loc["C1", "importe"] == 400


def test_close_rate_is_won_over_decided_by_count_and_by_money():
    f = m.quote_folios(
        quotes([q("C1", 1, "Ganada", 100), q("C2", 1, "Perdida", 300), q("C3", 1, "Abierta", 999)])
    )
    s = m.quote_summary(f, as_of=ts("2026-03-20"))
    assert s.close_rate == 0.5  # C3 is not decided yet
    assert s.close_rate_value == pytest.approx(100 / 400)
    assert s.days_to_close == 10
    assert (s.open_count, s.cold_count) == (1, 0)
    assert m.quote_summary(f, as_of=ts("2026-06-01")).cold_count == 1


def test_by_group_rates_are_sums_over_sums_not_averages():
    rows = [
        q(f"A{i}", 1, "Ganada" if i < 9 else "Perdida", 10, tipo="Diseñador") for i in range(10)
    ]
    rows += [q("B1", 1, "Perdida", 10, tipo="Particular")]
    f = m.quote_folios(quotes(rows))
    by = m.close_rate_by(f, "tipo_cliente").set_index("tipo_cliente")["tasa"]
    assert by.to_dict() == {"Diseñador": 0.9, "Particular": 0.0}
    assert m.quote_summary(f, ts("2026-04-01")).close_rate == pytest.approx(9 / 11)


def test_a_month_mostly_undecided_is_left_out_of_the_trend():
    rows = [
        q("M1", 1, "Ganada", 1, fecha="2026-01-10"),
        q("M2", 1, "Perdida", 1, fecha="2026-01-11"),
    ]
    rows += [
        q("F1", 1, "Ganada", 1, fecha="2026-02-10"),
        q("F2", 1, "Abierta", 1, fecha="2026-02-11"),
    ]
    t = m.close_rate_by_month(m.quote_folios(quotes(rows)))
    assert t["mes"].tolist() == [ts("2026-01-01")]  # February would show 100 %


def orders(rows):
    cols = [
        "folio_pedido",
        "partida",
        "fecha_pedido",
        "cliente",
        "vendedor",
        "marca",
        "pieza",
        "cantidad",
        "precio_venta",
        "fecha_prometida",
        "fecha_entrega",
        "folio_factura",
    ]
    df = pd.DataFrame(rows, columns=cols)
    for c in ("fecha_pedido", "fecha_prometida", "fecha_entrega"):
        df[c] = pd.to_datetime(df[c])
    return df


def o(folio, line, price, delivered, promised="2026-04-01", factura=None, marca="Bruma"):
    return [
        folio,
        line,
        "2026-03-01",
        "c",
        "Ana",
        marca,
        f"p{line}",
        1,
        price,
        promised,
        delivered,
        factura,
    ]


def test_an_order_is_on_time_only_if_its_last_piece_is():
    f = m.order_folios(
        orders(
            [
                o("P1", 1, 100, "2026-03-20"),
                o("P1", 2, 100, "2026-04-03"),  # 2 days late
                o("P2", 1, 100, "2026-03-30"),
                o("P3", 1, 100, None),  # open
            ]
        )
    ).set_index("folio_pedido")
    assert not f.loc["P1", "a_tiempo"] and f.loc["P1", "dias_retraso"] == 2
    assert f.loc["P2", "a_tiempo"]
    assert pd.isna(f.loc["P3", "a_tiempo"]) and not f.loc["P3", "entregado"]
    s = m.delivery_summary(f.reset_index())
    assert (s.delivered, s.on_time, s.avg_days_late, s.open_orders) == (2, 0.5, 2.0, 1)


def costs(rows):
    return pd.DataFrame(
        rows, columns=["folio_pedido", "partida", "concepto", "importe", "fecha", "proveedor"]
    )


def test_order_level_costs_are_spread_by_price_and_missing_costs_stay_missing():
    od = orders(
        [
            o("P1", 1, 300, "2026-03-20"),
            o("P1", 2, 100, "2026-03-20"),
            o("P2", 1, 500, "2026-03-20"),
        ]
    )
    c = costs(
        [
            ["P1", 1, "Materiales", 100.0, None, None],
            ["P1", None, "Flete", 40.0, None, None],  # whole order: 30 to line 1, 10 to line 2
        ]
    )
    lines = m.line_margin(od, c).set_index(["folio_pedido", "partida"])
    assert lines.loc[("P1", 1), "costo"] == pytest.approx(130)
    assert lines.loc[("P1", 2), "costo"] == pytest.approx(10)
    assert np.isnan(lines.loc[("P2", 1), "costo"])  # never 100 % margin
    total = m.margin_by(lines.reset_index().assign(t=1), "t").iloc[0]
    assert total["ventas"] == 400 and total["margen_pct"] == pytest.approx(1 - 140 / 400)


def test_invoices_match_however_the_folio_was_typed():
    od = orders(
        [
            o("P1", 1, 100, "2026-03-20", factura="a-77"),
            o("P2", 1, 200, "2026-03-20", factura="6f1e4f5a-1b2c-4d3e-9f8a-0123456789ab"),
            o("P3", 1, 300, "2026-03-20"),  # delivered, never invoiced
            o("P4", 1, 400, None),  # not delivered: nothing to invoice yet
        ]
    )
    inv = pd.DataFrame(
        [
            {
                "uuid": "AAAA-1",
                "serie": "A",
                "folio": "77",
                "folio_key": "A77",
                "tipo": "I",
                "subtotal_mxn": 100.0,
                "fecha": ts("2026-03-21"),
            },
            {
                "uuid": "6F1E4F5A-1B2C-4D3E-9F8A-0123456789AB",
                "serie": "B",
                "folio": "9",
                "folio_key": "B9",
                "tipo": "I",
                "subtotal_mxn": 250.0,
                "fecha": ts("2026-03-21"),
            },
            {
                "uuid": "ZZZZ",
                "serie": "A",
                "folio": "99",
                "folio_key": "A99",
                "tipo": "I",
                "subtotal_mxn": 50.0,
                "fecha": ts("2026-03-22"),
            },
        ]
    )
    matched, orphan = m.match_invoices(od, inv)
    matched = matched.set_index("folio_pedido")
    assert matched.loc["P1", "uuid"] == "AAAA-1"
    assert matched.loc["P2", "facturado"] == 250.0
    assert m.to_invoice(matched.reset_index(), ts("2026-04-01"))["folio_pedido"].tolist() == ["P3"]
    assert orphan["uuid"].tolist() == ["ZZZZ"]
    assert m.amount_mismatches(matched.reset_index())["folio_pedido"].tolist() == ["P2"]


def test_stage_summary_counts_finished_and_in_progress():
    pr = pd.DataFrame(
        {
            "folio_pedido": ["P1", "P1", "P2"],
            "partida": [1, 1, 1],
            "etapa": ["Carpintería", "Tapicería", "Tapicería"],
            "fecha_inicio": pd.to_datetime(["2026-03-01", "2026-03-10", "2026-03-15"]),
            "fecha_fin": pd.to_datetime(["2026-03-09", "2026-03-14", None]),
        }
    )
    s = m.stage_summary(pr, ts("2026-03-20")).set_index("etapa")
    assert s.loc["Carpintería", "mediana"] == 8 and s.loc["Tapicería", "mediana"] == 4
    assert s.loc["Tapicería", "en_proceso"] == 1 and s.loc["Tapicería", "dias_en_proceso"] == 5
