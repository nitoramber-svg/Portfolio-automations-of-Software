"""The late-order estimate: hand-made cases, then checked against the demo's own history."""

from __future__ import annotations

import pandas as pd

from mto_bi import risk
from mto_bi.schema import STAGES
from tests.conftest import AS_OF, ts

BUY, CARP, UPH, FIN, QC = STAGES


def shop(history_routes, open_lines):
    """Ten delivered tables (no upholstery) and sofas, each stage 4 days, then the open lines."""
    orders, prod = [], []
    n = 0
    for piece, route in history_routes.items():
        for _ in range(10):
            n += 1
            start = ts("2026-01-05") + pd.Timedelta(days=n)
            t = start + pd.Timedelta(days=1)
            for stage in route:
                prod.append([f"H{n}", 1, stage, t, t + pd.Timedelta(days=4)])
                t += pd.Timedelta(days=5)
            orders.append(
                [
                    f"H{n}",
                    1,
                    "c",
                    "Ana",
                    "Bruma",
                    piece,
                    start,
                    start + pd.Timedelta(days=40),
                    t + pd.Timedelta(days=1),
                ]
            )
    for folio, piece, ordered, promised, stages in open_lines:
        orders.append([folio, 1, "c", "Ana", "Bruma", piece, ts(ordered), ts(promised), pd.NaT])
        for stage, start, end in stages:
            prod.append([folio, 1, stage, ts(start), ts(end) if end else pd.NaT])
    o = pd.DataFrame(
        orders,
        columns=[
            "folio_pedido",
            "partida",
            "cliente",
            "vendedor",
            "marca",
            "pieza",
            "fecha_pedido",
            "fecha_prometida",
            "fecha_entrega",
        ],
    )
    p = pd.DataFrame(
        prod, columns=["folio_pedido", "partida", "etapa", "fecha_inicio", "fecha_fin"]
    )
    return o, p


ROUTES = {"Mesa": (BUY, CARP, FIN, QC), "Sofá": (BUY, CARP, UPH, QC)}


def test_a_table_never_waits_for_upholstery():
    o, p = shop(
        ROUTES, [("T1", "Mesa", "2026-03-01", "2026-04-30", [(BUY, "2026-03-02", "2026-03-06")])]
    )
    r = risk.at_risk(o, p, ts("2026-03-07")).set_index("folio_pedido").loc["T1"]
    assert "Tapicería" not in r["motivo"] and "Acabado" in r["motivo"]
    assert r["etapa_actual"] == "Por entrar a Carpintería"


def test_a_piece_stuck_in_a_stage_is_flagged_and_estimated_late():
    o, p = shop(
        ROUTES,
        [
            (
                "S1",
                "Sofá",
                "2026-03-01",
                "2026-03-25",
                [(BUY, "2026-03-02", "2026-03-06"), (CARP, "2026-03-07", None)],
            )
        ],
    )
    r = risk.at_risk(o, p, ts("2026-03-20")).set_index("folio_pedido").loc["S1"]
    assert "más lento de lo normal" in r["motivo"]
    assert r["estado"] == risk.AT_RISK and r["holgura"] < 0


def test_past_the_promise_is_overdue_whatever_the_estimate():
    o, p = shop(ROUTES, [("S2", "Sofá", "2026-03-01", "2026-03-10", [])])
    assert (
        risk.at_risk(o, p, ts("2026-03-11")).set_index("folio_pedido").loc["S2", "estado"]
        == risk.LATE
    )


def test_a_finished_piece_only_waits_for_delivery():
    stages = [(s, "2026-03-02", "2026-03-04") for s in ROUTES["Mesa"]]
    o, p = shop(ROUTES, [("T2", "Mesa", "2026-03-01", "2026-04-30", stages)])
    r = risk.at_risk(o, p, ts("2026-03-05")).set_index("folio_pedido").loc["T2"]
    assert r["etapa_actual"] == "Listo para entregar" and r["motivo"] == "solo falta entregar"


def test_snapshot_forgets_the_future(demo):
    o, p = risk.snapshot(demo.orders, demo.production, ts("2026-03-15"))
    assert o["fecha_pedido"].max() <= ts("2026-03-15")
    assert o["fecha_entrega"].max() <= ts("2026-03-15")
    assert p["fecha_fin"].max() <= ts("2026-03-15")


def test_the_estimate_beats_guessing_on_the_demo_history(demo):
    """Acceptance: on the 15th of each of 12 past months, using only what was known that day."""
    end = pd.Timestamp(AS_OF) - pd.Timedelta(days=45)
    cutoffs = pd.date_range(end - pd.DateOffset(months=11), end, freq="MS") + pd.Timedelta(days=14)
    bt = risk.backtest(demo.orders, demo.production, cutoffs)
    assert len(bt) > 150
    hit = (bt["estimado_tarde"] & bt["fue_tarde"]).sum()
    precision = hit / bt["estimado_tarde"].sum()
    recall = hit / bt["fue_tarde"].sum()
    base = bt["fue_tarde"].mean()
    assert precision >= 2 * base, (precision, base)
    assert recall >= 0.35, recall
    assert bt["error_dias"].abs().median() <= 4


def test_statuses_sort_worst_first(demo):
    r = risk.at_risk(demo.orders, demo.production, AS_OF)
    assert len(r) == demo.orders["fecha_entrega"].isna().sum()
    assert r["estado"].map(risk.STATUS_ORDER.index).is_monotonic_increasing
