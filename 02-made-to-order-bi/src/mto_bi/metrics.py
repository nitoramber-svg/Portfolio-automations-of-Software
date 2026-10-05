"""The numbers: close rate, sales, on-time delivery, shop-floor times, margin and billing.

Plain functions over the dataset's tables, so every number can be tested with a hand-made
table. Rates are Σ/Σ (won quotes over closed quotes), never an average of percentages.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from mto_bi.schema import STAGES, folio_key

COLD_DAYS = 45  # an open quote nobody has closed after this many days is going cold
AMOUNT_BANDS = (0, 50_000, 150_000, 300_000, np.inf)
AMOUNT_LABELS = ("Hasta 50 mil", "50 a 150 mil", "150 a 300 mil", "Más de 300 mil")


def ratio(num, den) -> float:
    return float(num) / float(den) if den else float("nan")


def in_period(df: pd.DataFrame, column: str, start, end) -> pd.DataFrame:
    if df.empty or start is None:
        return df
    d = df[column]
    return df[(d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end))]


# --- quotes ------------------------------------------------------------------------------------


def quote_folios(quotes: pd.DataFrame) -> pd.DataFrame:
    """One row per quote. A quote is won if any of its pieces was won, open while any piece is
    still open and none was won, and lost otherwise. Its brand is the one with the most money."""
    if quotes.empty:
        return pd.DataFrame(
            columns=[
                "folio",
                "fecha",
                "cliente",
                "tipo_cliente",
                "vendedor",
                "marca",
                "importe",
                "importe_ganado",
                "estado",
                "fecha_cierre",
                "rango",
            ]
        )
    q = quotes.assign(ganado=quotes["importe"].where(quotes["estado"] == "Ganada", 0.0))
    top_brand = (
        (q.groupby(["folio", "marca"])["importe"].sum().reset_index().sort_values("importe"))
        .drop_duplicates("folio", keep="last")
        .set_index("folio")["marca"]
    )
    g = q.groupby("folio")
    out = g.agg(
        fecha=("fecha", "min"),
        cliente=("cliente", "first"),
        tipo_cliente=("tipo_cliente", "first"),
        vendedor=("vendedor", "first"),
        importe=("importe", "sum"),
        importe_ganado=("ganado", "sum"),
        fecha_cierre=("fecha_cierre", "max"),
        any_won=("estado", lambda s: (s == "Ganada").any()),
        any_open=("estado", lambda s: (s == "Abierta").any()),
    )
    out["marca"] = top_brand
    out["estado"] = np.where(
        out["any_won"], "Ganada", np.where(out["any_open"], "Abierta", "Perdida")
    )
    out["tipo_cliente"] = out["tipo_cliente"].fillna("Sin dato")
    out["rango"] = pd.cut(out["importe"], AMOUNT_BANDS, labels=AMOUNT_LABELS, right=False)
    return out.drop(columns=["any_won", "any_open"]).reset_index()


@dataclass(frozen=True)
class QuoteSummary:
    quotes: int
    closed: int
    won: int
    close_rate: float  # won / closed, by count
    value_closed: float
    value_won: float
    close_rate_value: float  # money won / money closed
    days_to_close: float  # median, won quotes
    open_value: float
    open_count: int
    cold_count: int
    cold_value: float


def quote_summary(folios: pd.DataFrame, as_of) -> QuoteSummary:
    closed = folios[folios["estado"] != "Abierta"]
    won = folios[folios["estado"] == "Ganada"]
    open_ = folios[folios["estado"] == "Abierta"]
    cold = open_[(pd.Timestamp(as_of) - open_["fecha"]).dt.days > COLD_DAYS] if as_of else open_[:0]
    days = (won["fecha_cierre"] - won["fecha"]).dt.days.dropna()
    return QuoteSummary(
        quotes=len(folios),
        closed=len(closed),
        won=len(won),
        close_rate=ratio(len(won), len(closed)),
        value_closed=float(closed["importe"].sum()),
        value_won=float(closed["importe_ganado"].sum()),
        close_rate_value=ratio(closed["importe_ganado"].sum(), closed["importe"].sum()),
        days_to_close=float(days.median()) if len(days) else float("nan"),
        open_value=float(open_["importe"].sum()),
        open_count=len(open_),
        cold_count=len(cold),
        cold_value=float(cold["importe"].sum()),
    )


def close_rate_by(folios: pd.DataFrame, key: str) -> pd.DataFrame:
    closed = folios[folios["estado"] != "Abierta"]
    if closed.empty:
        return pd.DataFrame(columns=[key, "cerradas", "ganadas", "tasa", "monto_ganado"])
    g = closed.groupby(key, observed=True)
    out = g.agg(
        cerradas=("folio", "size"),
        ganadas=("estado", lambda s: (s == "Ganada").sum()),
        monto_ganado=("importe_ganado", "sum"),
    ).reset_index()
    out["tasa"] = out["ganadas"] / out["cerradas"]
    return out.sort_values("tasa", ascending=False)


def close_rate_by_month(folios: pd.DataFrame, max_open: float = 0.15) -> pd.DataFrame:
    """Close rate by the month each quote was sent.

    A month enters only once most of its quotes are decided: wins are decided faster than
    losses, so a recent month with half its quotes open shows only the quick wins and looks
    like a record (measured on the demo: 64 % for the last month, in a year that ran at 38 %).
    """
    f = folios.assign(mes=folios["fecha"].dt.to_period("M").dt.to_timestamp())
    out = f.groupby("mes").agg(
        total=("folio", "size"),
        abiertas=("estado", lambda s: (s == "Abierta").sum()),
        ganadas=("estado", lambda s: (s == "Ganada").sum()),
    )
    out["cerradas"] = out["total"] - out["abiertas"]
    out["tasa"] = out["ganadas"] / out["cerradas"].where(out["cerradas"] > 0)
    out = out[(out["abiertas"] / out["total"]) <= max_open]
    return out.reset_index()[["mes", "cerradas", "ganadas", "tasa"]]


def loss_reasons(quotes: pd.DataFrame) -> pd.DataFrame:
    lost = quotes[quotes["estado"] == "Perdida"]
    if lost.empty:
        return pd.DataFrame(columns=["motivo", "importe", "parte"])
    out = (
        lost.assign(motivo=lost["motivo_perdida"].fillna("Sin motivo"))
        .groupby("motivo")["importe"]
        .sum()
        .sort_values(ascending=False)
        .reset_index()
    )
    out["parte"] = out["importe"] / out["importe"].sum()
    return out


def cold_quotes(folios: pd.DataFrame, as_of) -> pd.DataFrame:
    open_ = folios[folios["estado"] == "Abierta"].copy()
    open_["dias"] = (pd.Timestamp(as_of) - open_["fecha"]).dt.days
    return open_[open_["dias"] > COLD_DAYS].sort_values("importe", ascending=False)


# --- orders and delivery -----------------------------------------------------------------------


def order_folios(orders: pd.DataFrame) -> pd.DataFrame:
    """One row per order: delivered when its last piece is, promised by its latest promise."""
    if orders.empty:
        return pd.DataFrame(
            columns=[
                "folio_pedido",
                "fecha_pedido",
                "cliente",
                "vendedor",
                "marca",
                "piezas",
                "precio_venta",
                "fecha_prometida",
                "fecha_entrega",
                "entregado",
                "a_tiempo",
                "dias_retraso",
            ]
        )
    g = orders.groupby("folio_pedido")
    out = g.agg(
        fecha_pedido=("fecha_pedido", "min"),
        cliente=("cliente", "first"),
        vendedor=("vendedor", "first"),
        marca=("marca", "first"),
        piezas=("cantidad", "sum"),
        fecha_prometida=("fecha_prometida", "max"),
        fecha_entrega=("fecha_entrega", "max"),
        pendientes=("fecha_entrega", lambda s: s.isna().sum()),
    )
    if "precio_venta" in orders.columns:
        out["precio_venta"] = g["precio_venta"].sum()
    out["entregado"] = out["pendientes"] == 0
    out.loc[~out["entregado"], "fecha_entrega"] = pd.NaT
    late = (out["fecha_entrega"] - out["fecha_prometida"]).dt.days
    out["dias_retraso"] = late.clip(lower=0).where(out["entregado"])
    out["a_tiempo"] = (late <= 0).where(out["entregado"])
    return out.drop(columns=["pendientes"]).reset_index()


@dataclass(frozen=True)
class SalesSummary:
    sales: float
    orders: int
    pieces: int
    ticket: float
    top5_share: float  # share of sales from the five biggest clients


def sales_summary(orders: pd.DataFrame) -> SalesSummary:
    if orders.empty:
        return SalesSummary(0.0, 0, 0, float("nan"), float("nan"))
    sales = float(orders["precio_venta"].sum()) if "precio_venta" in orders else float("nan")
    n = orders["folio_pedido"].nunique()
    by_client = (
        orders.groupby("cliente")["precio_venta"].sum().sort_values(ascending=False)
        if "precio_venta" in orders
        else pd.Series(dtype=float)
    )
    return SalesSummary(
        sales=sales,
        orders=n,
        pieces=int(orders["cantidad"].sum()),
        ticket=ratio(sales, n),
        top5_share=ratio(by_client.head(5).sum(), by_client.sum()),
    )


def monthly(df: pd.DataFrame, date_col: str, value: str, by: str | None = None) -> pd.DataFrame:
    d = df.dropna(subset=[date_col]).copy()
    d["mes"] = d[date_col].dt.to_period("M").dt.to_timestamp()
    keys = ["mes", by] if by else ["mes"]
    return d.groupby(keys)[value].sum().reset_index()


@dataclass(frozen=True)
class DeliverySummary:
    delivered: int
    on_time: float
    avg_days_late: float  # among late orders
    promised_days: float  # median, order to promise
    actual_days: float  # median, order to delivery
    open_orders: int


def delivery_summary(folios: pd.DataFrame) -> DeliverySummary:
    done = folios[folios["entregado"]]
    late = done[done["a_tiempo"] == False]  # noqa: E712 - a_tiempo holds NaN for open orders
    return DeliverySummary(
        delivered=len(done),
        on_time=ratio((done["a_tiempo"] == True).sum(), len(done)),  # noqa: E712
        avg_days_late=float(late["dias_retraso"].mean()) if len(late) else 0.0,
        promised_days=float((done["fecha_prometida"] - done["fecha_pedido"]).dt.days.median()),
        actual_days=float((done["fecha_entrega"] - done["fecha_pedido"]).dt.days.median()),
        open_orders=int((~folios["entregado"]).sum()),
    )


def on_time_by(folios: pd.DataFrame, key: str) -> pd.DataFrame:
    done = folios[folios["entregado"]].copy()
    if done.empty:
        return pd.DataFrame(columns=[key, "entregados", "a_tiempo", "tasa", "dias_retraso"])
    done["ok"] = done["a_tiempo"].astype(bool)
    if key == "mes":
        done["mes"] = done["fecha_entrega"].dt.to_period("M").dt.to_timestamp()
    out = done.groupby(key).agg(
        entregados=("folio_pedido", "size"),
        a_tiempo=("ok", "sum"),
        dias_retraso=("dias_retraso", lambda s: s[s > 0].mean() if (s > 0).any() else 0.0),
    )
    out["tasa"] = out["a_tiempo"] / out["entregados"]
    return out.reset_index()


# --- shop floor --------------------------------------------------------------------------------


def stage_times(production: pd.DataFrame) -> pd.DataFrame:
    """Finished stages with their duration in days."""
    done = production.dropna(subset=["fecha_fin"]).copy()
    done["dias"] = (done["fecha_fin"] - done["fecha_inicio"]).dt.days.clip(lower=0)
    return done


def stage_summary(production: pd.DataFrame, as_of) -> pd.DataFrame:
    """Per stage: typical time, slow cases, and what is sitting there right now."""
    done = stage_times(production)
    open_ = production[production["fecha_fin"].isna()].copy()
    open_["dias_en_etapa"] = (pd.Timestamp(as_of) - open_["fecha_inicio"]).dt.days
    rows = []
    for stage in STAGES:
        d = done.loc[done["etapa"] == stage, "dias"]
        o = open_[open_["etapa"] == stage]
        rows.append(
            {
                "etapa": stage,
                "terminadas": len(d),
                "mediana": float(d.median()) if len(d) else np.nan,
                "p80": float(d.quantile(0.8)) if len(d) else np.nan,
                "en_proceso": len(o),
                "dias_en_proceso": float(o["dias_en_etapa"].mean()) if len(o) else np.nan,
            }
        )
    return pd.DataFrame(rows)


def stage_by_month(production: pd.DataFrame) -> pd.DataFrame:
    done = stage_times(production)
    done["mes"] = done["fecha_inicio"].dt.to_period("M").dt.to_timestamp()
    return done.groupby(["mes", "etapa"])["dias"].median().reset_index()


# --- margin ------------------------------------------------------------------------------------


def line_margin(orders: pd.DataFrame, costs: pd.DataFrame) -> pd.DataFrame:
    """Each order line with its cost and margin. Costs recorded for a whole order (no partida)
    are spread over its lines by price. Lines with no cost at all keep cost NaN: they are
    reported as missing, never as 100 % margin."""
    lines = orders.copy()
    lines["costo"] = 0.0
    if costs.empty:
        lines["costo"] = np.nan
        lines["margen"] = np.nan
        lines["margen_pct"] = np.nan
        return lines
    per_line = costs.dropna(subset=["partida"])
    direct: dict[tuple[str, int], float] = {}
    for folio, line, amount in zip(
        per_line["folio_pedido"], per_line["partida"], per_line["importe"], strict=True
    ):
        direct[(folio, int(line))] = direct.get((folio, int(line)), 0.0) + float(amount)
    whole = costs[costs["partida"].isna()].groupby("folio_pedido")["importe"].sum()
    lines["costo_directo"] = [
        direct.get((f, int(p)), np.nan)
        for f, p in zip(lines["folio_pedido"], lines["partida"], strict=True)
    ]
    share = lines["precio_venta"] / lines.groupby("folio_pedido")["precio_venta"].transform("sum")
    lines["costo_prorrateado"] = lines["folio_pedido"].map(whole) * share
    has = lines["costo_directo"].notna() | lines["costo_prorrateado"].notna()
    lines["costo"] = (
        lines["costo_directo"].fillna(0) + lines["costo_prorrateado"].fillna(0)
    ).where(has)
    lines["margen"] = lines["precio_venta"] - lines["costo"]
    lines["margen_pct"] = lines["margen"] / lines["precio_venta"]
    return lines


def margin_by(lines: pd.DataFrame, key: str) -> pd.DataFrame:
    with_cost = lines.dropna(subset=["costo"])
    if with_cost.empty:
        return pd.DataFrame(columns=[key, "ventas", "costo", "margen", "margen_pct", "piezas"])
    if key == "mes":
        with_cost = with_cost.assign(
            mes=with_cost["fecha_entrega"].dt.to_period("M").dt.to_timestamp()
        )
    out = with_cost.groupby(key).agg(
        ventas=("precio_venta", "sum"),
        costo=("costo", "sum"),
        piezas=("cantidad", "sum"),
    )
    out["margen"] = out["ventas"] - out["costo"]
    out["margen_pct"] = out["margen"] / out["ventas"]
    return out.reset_index()


def cost_mix(orders: pd.DataFrame, costs: pd.DataFrame, by: str = "marca") -> pd.DataFrame:
    """Cost per concept as a share of sales, by brand: where the money goes."""
    if costs.empty:
        return pd.DataFrame(columns=[by, "concepto", "parte"])
    lines = orders[["folio_pedido", "partida", by, "precio_venta"]]
    c = costs.copy()
    order_brand = lines.groupby("folio_pedido")[by].first()
    c[by] = c["folio_pedido"].map(order_brand)
    sales = (
        lines[lines["folio_pedido"].isin(set(c["folio_pedido"]))].groupby(by)["precio_venta"].sum()
    )
    out = c.groupby([by, "concepto"])["importe"].sum().reset_index()
    out["parte"] = out["importe"] / out[by].map(sales)
    return out


# --- billing -----------------------------------------------------------------------------------


def match_invoices(
    orders: pd.DataFrame, invoices: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(orders with their invoice, invoices with no order). An order points at its invoice by
    serie-folio or UUID, typed any way: A-1234, a1234 and the UUID all match."""
    folios = order_folios(orders)
    keys = orders.dropna(subset=["folio_factura"]).groupby("folio_pedido")["folio_factura"].first()
    folios["factura"] = folios["folio_pedido"].map(keys)
    folios["factura_key"] = folios["factura"].map(
        lambda v: folio_key(v) if isinstance(v, str) else None
    )
    inv = invoices.copy()
    inv["uuid_key"] = inv["uuid"].map(folio_key)
    by_folio = inv.set_index("folio_key")
    by_uuid = inv.set_index("uuid_key")
    found = []
    for k in folios["factura_key"]:
        if not k:
            found.append(None)
        elif k in by_uuid.index:
            found.append(by_uuid.loc[k, "uuid"])
        elif k in by_folio.index:
            hit = by_folio.loc[k, "uuid"]
            found.append(hit if isinstance(hit, str) else hit.iloc[0])
        else:
            found.append(None)
    folios["uuid"] = found
    sub = inv.set_index("uuid")["subtotal_mxn"]
    folios["facturado"] = folios["uuid"].map(sub)
    orphan = inv[~inv["uuid"].isin(set(folios["uuid"].dropna())) & (inv["tipo"] == "I")]
    return folios, orphan


def billing_by_month(invoices: pd.DataFrame) -> pd.DataFrame:
    inv = invoices.copy()
    inv["neto"] = np.where(inv["tipo"] == "E", -inv["subtotal_mxn"], inv["subtotal_mxn"])
    inv["mes"] = inv["fecha"].dt.to_period("M").dt.to_timestamp()
    return inv.groupby("mes")["neto"].sum().reset_index()


def to_invoice(matched: pd.DataFrame, as_of) -> pd.DataFrame:
    """Delivered orders with no invoice found: money that should already be billed."""
    pending = matched[matched["entregado"] & matched["uuid"].isna()].copy()
    pending["dias_desde_entrega"] = (pd.Timestamp(as_of) - pending["fecha_entrega"]).dt.days
    return pending.sort_values("dias_desde_entrega", ascending=False)


def amount_mismatches(matched: pd.DataFrame, tolerance: float = 0.01) -> pd.DataFrame:
    m = matched.dropna(subset=["facturado"]).copy()
    m["diferencia"] = m["facturado"] - m["precio_venta"]
    return m[(m["diferencia"].abs() / m["precio_venta"]) > tolerance]
