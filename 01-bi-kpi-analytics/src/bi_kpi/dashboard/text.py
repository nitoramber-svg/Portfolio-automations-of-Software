"""Formatting and the plain-language reading of the numbers (for non-technical readers)."""

from __future__ import annotations

import math

import pandas as pd

SYMBOLS = {"BRL": "R$", "MXN": "MX$", "USD": "US$"}
MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def _missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def money(value, currency: str, compact: bool = True) -> str:
    if _missing(value):
        return "—"
    symbol = SYMBOLS[currency]
    if compact and abs(value) >= 1_000_000:
        return f"{symbol} {value / 1_000_000:,.2f} M"
    if compact and abs(value) >= 100_000:
        return f"{symbol} {value / 1_000:,.0f} mil"
    if compact and abs(value) >= 10_000:
        return f"{symbol} {value / 1_000:,.1f} mil"
    return f"{symbol} {value:,.2f}"


def fmt(value, unit: str, currency: str = "BRL") -> str:
    """A KPI value in its unit."""
    if _missing(value):
        return "—"
    if unit == "currency":
        return money(value, currency)
    if unit == "ratio":
        return f"{value:.1%}"
    if unit == "count":
        return f"{value:,.0f}"
    if unit == "score":
        return f"{value:.2f} ★"
    return f"{value:,.1f}"


def month_label(year_month) -> str:
    """201803 -> 'mar 2018'."""
    ym = int(year_month)
    return f"{MONTHS[ym % 100 - 1]} {ym // 100}"


def narrative(
    values: dict[str, float],
    by_region: pd.DataFrame,
    by_month: pd.DataFrame,
    currency: str,
) -> list[str]:
    """Sentences that read the selection: what sold, against plan, where, and what went wrong.

    ``values``: KPI id -> value for the selection. ``by_region`` / ``by_month``: columns key,
    gmv, on_time_delivery, avg_score (as returned by data.kpi_by). Sentences whose numbers
    are missing (a seller has no plan) are left out rather than written with blanks.
    """
    out = []
    gmv, orders = values.get("gmv"), values.get("orders")
    if not _missing(gmv) and not _missing(orders) and orders:
        out.append(
            f"Se vendieron {money(gmv, currency)} en {orders:,.0f} pedidos "
            f"(ticket promedio {money(values.get('avg_ticket'), currency, compact=False)})."
        )
    att = values.get("plan_attainment")
    if not _missing(att):
        verdict = "por encima" if att >= 1 else "por debajo"
        out.append(f"Eso es {att:.0%} del plan de ventas, {verdict} de lo esperado.")

    regions = by_region.dropna(subset=["gmv"])
    if len(regions) > 1 and regions["gmv"].sum() > 0:
        top = regions.loc[regions["gmv"].idxmax()]
        share = top["gmv"] / regions["gmv"].sum()
        out.append(f"{top['key']} concentra {share:.0%} de las ventas.")

    otd = values.get("on_time_delivery")
    if not _missing(otd):
        sentence = f"Entregas a tiempo: {otd:.1%} (meta 90 %)"
        late = regions.dropna(subset=["on_time_delivery"])
        late = late[late["on_time_delivery"] < 0.90]
        if len(regions) > 1 and len(late):
            worst = late.loc[late["on_time_delivery"].idxmin()]
            sentence += f"; {worst['key']} queda abajo con {worst['on_time_delivery']:.1%}"
        out.append(sentence + ".")

    months = by_month.dropna(subset=["on_time_delivery"])
    if len(months) > 2:
        worst = months.loc[months["on_time_delivery"].idxmin()]
        if worst["on_time_delivery"] < 0.90:
            score = worst.get("avg_score")
            tail = "" if _missing(score) else f" y la calificación bajó a {score:.2f}"
            out.append(
                f"El peor mes en puntualidad fue {month_label(worst['key'])}: "
                f"{worst['on_time_delivery']:.1%} a tiempo{tail}."
            )

    score, neg = values.get("avg_score"), values.get("negative_reviews")
    if not _missing(score) and not _missing(neg):
        out.append(
            f"Calificación promedio {score:.2f} de 5; {neg:.1%} de las reseñas son negativas."
        )
    return out
