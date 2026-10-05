"""Which open orders will arrive late, and why.

For each piece not yet delivered: the stages it still has to go through, at the pace the shop
has had lately (the median of each stage over the last ``WINDOW_DAYS``), plus the usual wait
between stages and before delivery. That gives an estimated delivery date to hold against the
promise.

The route of a piece is the one that same piece followed before (a table never goes through
upholstery); a piece never made before follows its brand's usual route.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from mto_bi.metrics import stage_times
from mto_bi.schema import STAGES

WINDOW_DAYS = 120
MIN_SAMPLES = 5
TIGHT_DAYS = 5  # less slack than this is "justo"
LATE, AT_RISK, TIGHT, OK = "Vencido", "Va tarde", "Justo", "En tiempo"
STATUS_ORDER = (LATE, AT_RISK, TIGHT, OK)


@dataclass(frozen=True)
class Pace:
    stage_days: dict[str, float]  # median days per stage, recent
    gap_days: float  # median wait between one stage and the next
    start_days: float  # median days from order to first stage
    delivery_days: float  # median days from last stage to delivery
    routes_by_piece: dict[str, tuple[str, ...]]
    routes_by_brand: dict[str, tuple[str, ...]]


def pace(orders: pd.DataFrame, production: pd.DataFrame, as_of) -> Pace:
    as_of = pd.Timestamp(as_of)
    done = stage_times(production)
    recent = done[done["fecha_fin"] >= as_of - pd.Timedelta(days=WINDOW_DAYS)]
    stage_days = {}
    for stage in STAGES:
        d = recent.loc[recent["etapa"] == stage, "dias"]
        if len(d) < MIN_SAMPLES:
            d = done.loc[done["etapa"] == stage, "dias"]
        stage_days[stage] = float(d.median()) if len(d) else np.nan

    ordered = done.assign(rank=done["etapa"].map(STAGES.index)).sort_values(
        ["folio_pedido", "partida", "rank"]
    )
    nxt = ordered.groupby(["folio_pedido", "partida"])["fecha_inicio"].shift(-1)
    gaps = (nxt - ordered["fecha_fin"]).dt.days.dropna().clip(lower=0)

    first = production.groupby(["folio_pedido", "partida"])["fecha_inicio"].min()
    last = done.groupby(["folio_pedido", "partida"])["fecha_fin"].max()
    lines = orders.set_index(["folio_pedido", "partida"])
    start = (first - lines["fecha_pedido"].reindex(first.index)).dt.days.dropna()
    delivered = lines["fecha_entrega"].dropna()
    tail = (delivered - last.reindex(delivered.index)).dt.days.dropna().clip(lower=0)

    # Routes from delivered pieces only: their history is complete.
    finished = production.set_index(["folio_pedido", "partida"]).loc[
        lambda p: p.index.isin(delivered.index)
    ]
    route_of_line = finished.groupby(level=[0, 1])["etapa"].agg(
        lambda s: tuple(x for x in STAGES if x in set(s))
    )
    info = lines.loc[route_of_line.index, ["pieza", "marca"]].assign(ruta=route_of_line)
    by_piece = info.groupby("pieza")["ruta"].agg(lambda s: s.mode().iloc[0]).to_dict()
    by_brand = info.groupby("marca")["ruta"].agg(lambda s: s.mode().iloc[0]).to_dict()

    def med(s, default):
        return float(s.median()) if len(s) else default

    return Pace(
        stage_days=stage_days,
        gap_days=med(gaps, 1.0),
        start_days=med(start, 2.0),
        delivery_days=med(tail, 3.0),
        routes_by_piece=by_piece,
        routes_by_brand=by_brand,
    )


def at_risk(orders: pd.DataFrame, production: pd.DataFrame, as_of) -> pd.DataFrame:
    """Every undelivered piece with its estimated delivery, slack, status and the reason."""
    as_of = pd.Timestamp(as_of)
    open_lines = orders[orders["fecha_entrega"].isna()]
    cols = [
        "folio_pedido",
        "partida",
        "cliente",
        "vendedor",
        "marca",
        "pieza",
        "fecha_pedido",
        "fecha_prometida",
        "etapa_actual",
        "estimada",
        "holgura",
        "estado",
        "motivo",
    ]
    if open_lines.empty:
        return pd.DataFrame(columns=cols)
    p = pace(orders, production, as_of)
    stages_of = {k: g for k, g in production.groupby(["folio_pedido", "partida"])}
    rows = []
    for r in open_lines.itertuples():
        hist = stages_of.get((r.folio_pedido, r.partida))
        route = p.routes_by_piece.get(r.pieza) or p.routes_by_brand.get(r.marca) or STAGES
        remaining, notes, current = _remaining(hist, route, p, as_of, r.fecha_pedido)
        eta = as_of + pd.Timedelta(days=round(remaining))
        slack = (r.fecha_prometida - eta).days
        if as_of > r.fecha_prometida:
            status = LATE
        elif slack < 0:
            status = AT_RISK
        elif slack < TIGHT_DAYS:
            status = TIGHT
        else:
            status = OK
        rows.append(
            {
                "folio_pedido": r.folio_pedido,
                "partida": r.partida,
                "cliente": r.cliente,
                "vendedor": r.vendedor,
                "marca": r.marca,
                "pieza": r.pieza,
                "fecha_pedido": r.fecha_pedido,
                "fecha_prometida": r.fecha_prometida,
                "etapa_actual": current,
                "estimada": eta,
                "holgura": slack,
                "estado": status,
                "motivo": notes,
            }
        )
    out = pd.DataFrame(rows, columns=cols)
    out["_o"] = out["estado"].map(STATUS_ORDER.index)
    return out.sort_values(["_o", "holgura"]).drop(columns="_o").reset_index(drop=True)


def _remaining(hist, route, p: Pace, as_of, ordered_on):
    """(days left, explanation, current stage) for one piece."""
    finished = set() if hist is None else set(hist.loc[hist["fecha_fin"].notna(), "etapa"])
    in_progress = None if hist is None else hist[hist["fecha_fin"].isna()]
    days, parts = 0.0, []
    current = "Sin empezar"
    if in_progress is not None and len(in_progress):
        row = in_progress.iloc[0]
        current = row["etapa"]
        typical = p.stage_days.get(current, np.nan)
        spent = (as_of - row["fecha_inicio"]).days
        left = max(typical - spent, 0.25 * typical, 1.0) if typical == typical else 1.0
        days += left
        over = " (ya va más lento de lo normal)" if spent > typical else ""
        parts.append(f"lleva {spent} d en {current}, normal {typical:.0f} d{over}")
    elif not finished:
        waited = (as_of - ordered_on).days
        days += max(p.start_days - waited, 0)
        if waited > p.start_days + 3:
            parts.append(f"lleva {waited} d sin entrar al taller")
    last_done = max((STAGES.index(s) for s in finished), default=-1)
    started = {current} | finished
    todo = [s for s in route if s not in started and STAGES.index(s) > last_done]
    if finished and current == "Sin empezar":
        current = f"Por entrar a {todo[0]}" if todo else "Listo para entregar"
    for s in todo:
        d = p.stage_days.get(s, np.nan)
        d = 3.0 if d != d else d
        days += p.gap_days + d
    if todo:
        parts.append("faltan " + ", ".join(f"{s} ({p.stage_days.get(s, 0):.0f} d)" for s in todo))
    days += p.delivery_days
    return days, "; ".join(parts) or "solo falta entregar", current


# --- checking the estimate against history ---------------------------------------------------


def snapshot(
    orders: pd.DataFrame, production: pd.DataFrame, cutoff
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The two tables as they looked on ``cutoff``: later dates had not happened yet."""
    cutoff = pd.Timestamp(cutoff)
    o = orders[orders["fecha_pedido"] <= cutoff].copy()
    o.loc[o["fecha_entrega"] > cutoff, "fecha_entrega"] = pd.NaT
    pr = production[production["fecha_inicio"] <= cutoff].copy()
    pr.loc[pr["fecha_fin"] > cutoff, "fecha_fin"] = pd.NaT
    return o, pr


def backtest(orders: pd.DataFrame, production: pd.DataFrame, cutoffs) -> pd.DataFrame:
    """For each past cutoff, what the estimate said for each open piece vs what happened.

    Pieces already overdue at the cutoff are left out: calling them late is no prediction.
    """
    truth = orders.set_index(["folio_pedido", "partida"])
    rows = []
    for cutoff in cutoffs:
        o, pr = snapshot(orders, production, cutoff)
        est = at_risk(o, pr, cutoff)
        est = est[est["estado"] != LATE]
        for r in est.itertuples():
            actual = truth.loc[(r.folio_pedido, r.partida), "fecha_entrega"]
            if pd.isna(actual):
                continue
            rows.append(
                {
                    "corte": pd.Timestamp(cutoff),
                    "estimado_tarde": r.estado == AT_RISK,
                    "fue_tarde": actual > r.fecha_prometida,
                    "error_dias": (actual - r.estimada).days,
                }
            )
    return pd.DataFrame(rows)
