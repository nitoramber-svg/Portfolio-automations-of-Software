"""One function per page. Each receives the context (who is looking, which period, the data
they may see) and draws its page; nothing here reads files or decides permissions."""

from __future__ import annotations

import io
import zipfile

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

from mto_bi import dataset, metrics, risk, roles, sample
from mto_bi.app import fmt
from mto_bi.app.main import Context, current_dataset
from mto_bi.schema import BY_NAME, STAGES, TEMPLATES
from mto_bi.templates import workbook_bytes

PALETTE = ("#1d5f8a", "#c2703d", "#5b8c5a", "#8a5a9e", "#b5a033", "#4b7f86")
GOOD, WARN, BAD, MUTED = "#3f8f5b", "#d39b2a", "#c8473b", "#9aa5b1"
STATUS_COLORS = {risk.LATE: BAD, risk.AT_RISK: "#e07b39", risk.TIGHT: WARN, risk.OK: GOOD}
STATUS_ICONS = {risk.LATE: "🔴", risk.AT_RISK: "🟠", risk.TIGHT: "🟡", risk.OK: "🟢"}
PLURAL = {
    "Diseñador": "diseñadores",
    "Arquitecto": "arquitectos",
    "Particular": "particulares",
    "Empresa": "empresas",
    "Sin dato": "clientes sin tipo",
}


# --- helpers -----------------------------------------------------------------------------------


def colors(values) -> dict:
    return {v: PALETTE[i % len(PALETTE)] for i, v in enumerate(sorted(set(values)))}


def chart(fig, height: int = 330, pct_y: bool = False, pct_x: bool = False):
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=36, b=8),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
        xaxis_title=None,
        yaxis_title=None,
        hoverlabel=dict(namelength=-1),
    )
    if pct_y:
        fig.update_yaxes(tickformat=".0%")
    if pct_x:
        fig.update_xaxes(tickformat=".0%")
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def complete_months(df: pd.DataFrame, as_of, column: str = "mes") -> pd.DataFrame:
    """Drop the month in progress: a half month always looks like a drop."""
    current = pd.Timestamp(as_of).to_period("M").to_timestamp()
    return df[df[column] < current]


def no_data(what: str, template: str | None = None):
    hint = f" Cárgala en **Cargar datos** con la plantilla de {template}." if template else ""
    st.info(f"Todavía no hay datos de {what}.{hint}", icon=":material/info:")


def header(title: str, ctx: Context, subtitle: str = ""):
    st.title(title)
    period = f"{fmt.day(ctx.start)} – {fmt.day(ctx.end)}"
    st.caption(fmt.md(f"{subtitle}  ·  Periodo: {period}" if subtitle else f"Periodo: {period}"))


def previous(ctx: Context, df: pd.DataFrame, column: str) -> pd.DataFrame:
    span = ctx.end - ctx.start
    d = df[column]
    return df[(d >= ctx.start - span) & (d < ctx.start)]


def delta(now, before, kind: str = "money"):
    if fmt.missing(now) or fmt.missing(before) or not before:
        return None
    if kind == "pp":
        return f"{(now - before) * 100:+.1f} pp vs periodo anterior"
    return f"{now / before - 1:+.0%} vs periodo anterior"


def csv_button(df: pd.DataFrame, name: str, label: str = "Descargar como Excel (CSV)"):
    st.download_button(
        label,
        df.to_csv(index=False).encode("utf-8-sig"),
        file_name=name,
        mime="text/csv",
        icon=":material/download:",
    )


@st.cache_data(show_spinner=False)
def _risk(_orders: pd.DataFrame, _production: pd.DataFrame, as_of, signature) -> pd.DataFrame:
    return risk.at_risk(_orders, _production, as_of)


@st.cache_data(show_spinner="Comprobando el estimador contra el historial…")
def _backtest(_orders: pd.DataFrame, _production: pd.DataFrame, as_of, signature):
    end = pd.Timestamp(as_of) - pd.Timedelta(days=45)
    cutoffs = pd.date_range(end - pd.DateOffset(months=11), end, freq="MS") + pd.Timedelta(days=14)
    bt = risk.backtest(_orders, _production, cutoffs)
    if bt.empty or not bt["estimado_tarde"].any() or not bt["fue_tarde"].any():
        return None
    hit = (bt["estimado_tarde"] & bt["fue_tarde"]).sum()
    return {
        "piezas": len(bt),
        "precision": hit / bt["estimado_tarde"].sum(),
        "recall": hit / bt["fue_tarde"].sum(),
        "base": bt["fue_tarde"].mean(),
        "error": float(bt["error_dias"].abs().median()),
    }


def _signature(ctx: Context) -> tuple:
    o, p = ctx.data.orders, ctx.data.production
    return (len(o), len(p), str(ctx.as_of), tuple(sorted(o["folio_pedido"].unique()))[:3])


def risk_table(ctx: Context) -> pd.DataFrame:
    return _risk(ctx.data.orders, ctx.data.production, ctx.as_of, _signature(ctx))


# --- summary -----------------------------------------------------------------------------------


def summary(ctx: Context):
    header("Resumen", ctx, "Lo más importante del periodo")
    d = ctx.data
    orders = ctx.period(d.orders, "fecha_pedido")
    folios = metrics.quote_folios(ctx.period(d.quotes, "fecha"))
    prev_folios = (
        metrics.quote_folios(previous(ctx, d.quotes, "fecha")) if len(d.quotes) else folios
    )
    all_orders = metrics.order_folios(d.orders)
    delivered = ctx.period(all_orders, "fecha_entrega")
    risky = risk_table(ctx)

    cols = st.columns(5)
    sales = metrics.sales_summary(orders)
    prev_sales = metrics.sales_summary(previous(ctx, d.orders, "fecha_pedido"))
    if "precio_venta" in d.orders:
        cols[0].metric(
            "Ventas",
            fmt.money(sales.sales),
            delta(sales.sales, prev_sales.sales),
            help="Pedidos confirmados en el periodo, sin IVA.",
        )
    if len(folios):
        qs, pq = (
            metrics.quote_summary(folios, ctx.as_of),
            metrics.quote_summary(prev_folios, ctx.as_of),
        )
        cols[1].metric(
            "Tasa de cierre",
            fmt.pct(qs.close_rate),
            delta(qs.close_rate, pq.close_rate, "pp"),
            help="Cotizaciones ganadas entre cotizaciones ya decididas (ganadas o perdidas).",
        )
    ds = metrics.delivery_summary(delivered) if len(delivered) else None
    if ds:
        cols[2].metric(
            "Entregas a tiempo",
            fmt.pct(ds.on_time),
            help="Pedidos entregados en el periodo en o antes de la fecha prometida.",
        )
    late = risky[risky["estado"].isin([risk.LATE, risk.AT_RISK])]
    cols[3].metric(
        "Piezas que van tarde",
        fmt.num(len(late)),
        help="Piezas sin entregar que ya pasaron su fecha o que, al ritmo actual del taller, "
        "no llegan. Detalle en «Pedidos en riesgo».",
    )
    lines = (
        metrics.line_margin(ctx.period(d.orders, "fecha_entrega"), d.costs)
        if len(d.costs)
        else None
    )
    if lines is not None and lines["costo"].notna().any():
        m = metrics.margin_by(lines.assign(todo="t"), "todo").iloc[0]
        cols[4].metric(
            "Margen bruto", fmt.pct(m["margen_pct"]), help="De pedidos entregados en el periodo."
        )

    st.subheader("Lectura del periodo")
    for sentence in reading(ctx, orders, folios, delivered, late, lines):
        st.markdown(fmt.md(f"- {sentence}"))

    if "precio_venta" in d.orders and len(orders):
        st.subheader("Ventas por mes")
        by = complete_months(
            metrics.monthly(orders, "fecha_pedido", "precio_venta", "marca"), ctx.as_of
        )
        fig = px.bar(
            by, x="mes", y="precio_venta", color="marca", color_discrete_map=colors(by["marca"])
        )
        fig.update_traces(hovertemplate="%{x|%b %Y}: $%{y:,.0f}")
        chart(fig)


def reading(ctx, orders, folios, delivered, late, lines) -> list[str]:
    out = []
    if "precio_venta" in orders and len(orders):
        s = metrics.sales_summary(orders)
        out.append(
            f"Se confirmaron **{fmt.num(s.orders)} pedidos** por **{fmt.money(s.sales)}** "
            f"(ticket promedio {fmt.money(s.ticket)})."
        )
    if len(folios):
        q = metrics.quote_summary(folios, ctx.as_of)
        if q.closed:
            text = (
                f"De cada 10 cotizaciones decididas se ganaron **{q.close_rate * 10:.1f}** "
                f"({fmt.num(q.won)} de {fmt.num(q.closed)})."
            )
            by = metrics.close_rate_by(folios, "tipo_cliente")
            by = by[by["cerradas"] >= 15]
            if len(by) >= 2:
                hi, lo = by.iloc[0], by.iloc[-1]
                text += (
                    f" Con {PLURAL.get(hi['tipo_cliente'], hi['tipo_cliente'])} se cierra el "
                    f"{fmt.pct(hi['tasa'])}; con {PLURAL.get(lo['tipo_cliente'], lo['tipo_cliente'])}, "
                    f"el {fmt.pct(lo['tasa'])}."
                )
            out.append(text)
        if q.cold_count:
            out.append(
                f"Hay **{q.cold_count} cotizaciones** abiertas por **{fmt.money(q.cold_value)}** "
                f"sin respuesta desde hace más de {metrics.COLD_DAYS} días: vale la pena llamarles."
            )
    if len(delivered):
        ds = metrics.delivery_summary(delivered)
        out.append(
            f"El **{fmt.pct(ds.on_time)}** de los pedidos llegó a tiempo; los que se atrasaron, "
            f"por {fmt.days(ds.avg_days_late)} en promedio."
        )
    stages = metrics.stage_summary(ctx.period(ctx.data.production, "fecha_inicio"), ctx.as_of)
    stages = stages.dropna(subset=["mediana"])
    if len(stages):
        slow = stages.loc[stages["mediana"].idxmax()]
        out.append(
            f"La etapa más lenta del taller es **{slow['etapa']}**: "
            f"{fmt.days(slow['mediana'])} típicos, {fmt.days(slow['p80'])} en los casos lentos."
        )
    if len(late):
        out.append(
            f"Hoy hay **{len(late)} piezas** que van tarde o ya vencieron. "
            "Están en «Pedidos en riesgo», con el motivo de cada una."
        )
    if lines is not None and lines["costo"].notna().any():
        drop = margin_drop(ctx)
        if drop:
            out.append(drop)
    if len(ctx.data.invoices):
        matched, _ = metrics.match_invoices(ctx.data.orders, ctx.data.invoices)
        pending = metrics.to_invoice(matched, ctx.as_of)
        if len(pending):
            out.append(
                f"Hay **{len(pending)} pedidos entregados sin factura** por "
                f"**{fmt.money(pending['precio_venta'].sum())}**."
            )
    return out


def margin_drop(ctx: Context) -> str | None:
    """The brand whose margin fell most: last six months against the six before."""
    lines = metrics.line_margin(ctx.data.orders, ctx.data.costs).dropna(
        subset=["costo", "fecha_entrega"]
    )
    cut = pd.Timestamp(ctx.as_of) - pd.DateOffset(months=6)
    recent, before = (
        lines[lines["fecha_entrega"] >= cut],
        lines[
            (lines["fecha_entrega"] < cut)
            & (lines["fecha_entrega"] >= cut - pd.DateOffset(months=6))
        ],
    )
    if recent.empty or before.empty:
        return None
    a = metrics.margin_by(before, "marca").set_index("marca")["margen_pct"]
    b = metrics.margin_by(recent, "marca").set_index("marca")["margen_pct"]
    change = (b - a).dropna()
    if change.empty or change.min() > -0.03:
        return None
    brand = change.idxmin()
    mix_before = _mix(before[before["marca"] == brand], ctx.data.costs)
    mix_recent = _mix(recent[recent["marca"] == brand], ctx.data.costs)
    rise = (mix_recent - mix_before).dropna()
    why = ""
    if len(rise) and rise.max() > 0.02:
        why = f" Lo que más subió fue **{rise.idxmax().lower()}**: de {fmt.pct(mix_before[rise.idxmax()])} a {fmt.pct(mix_recent[rise.idxmax()])} del precio."
    return (
        f"El margen de **{brand}** bajó de {fmt.pct(a[brand])} a **{fmt.pct(b[brand])}** "
        f"en los últimos seis meses.{why}"
    )


def _mix(lines: pd.DataFrame, costs: pd.DataFrame) -> pd.Series:
    keys = set(zip(lines["folio_pedido"], lines["partida"], strict=True))
    c = costs.dropna(subset=["partida"])
    c = c[[k in keys for k in zip(c["folio_pedido"], c["partida"].astype(int), strict=True)]]
    return c.groupby("concepto")["importe"].sum() / lines["precio_venta"].sum()


# --- quotes ------------------------------------------------------------------------------------


def quotes_page(ctx: Context):
    header("Cotizaciones", ctx, "Cuántas se ganan, de quién y por qué se pierden")
    q = ctx.period(ctx.data.quotes, "fecha")
    if q.empty:
        no_data("cotizaciones", "Cotizaciones")
        return
    folios = metrics.quote_folios(q)
    s = metrics.quote_summary(folios, ctx.as_of)
    c = st.columns(5)
    c[0].metric("Cotizaciones", fmt.num(s.quotes))
    c[1].metric(
        "Tasa de cierre", fmt.pct(s.close_rate), help="Ganadas entre decididas, por número."
    )
    c[2].metric(
        "Cierre por monto",
        fmt.pct(s.close_rate_value),
        help="Dinero ganado entre dinero decidido. Si es menor que la tasa por número, "
        "las cotizaciones grandes se pierden más.",
    )
    c[3].metric("Días para cerrar", fmt.num(s.days_to_close), help="Mediana, cotizaciones ganadas.")
    c[4].metric(
        "Abiertas",
        fmt.money(s.open_value),
        f"{s.open_count} cotizaciones",
        delta_color="off",
        delta_arrow="off",
    )

    left, right = st.columns(2)
    with left:
        st.subheader("Tasa de cierre por tipo de cliente")
        _rate_bar(metrics.close_rate_by(folios, "tipo_cliente"), "tipo_cliente")
        st.subheader("Por tamaño de la cotización")
        by = metrics.close_rate_by(folios, "rango").sort_values("rango")
        _rate_bar(by, "rango", sort=False)
    with right:
        st.subheader("Por vendedor")
        _rate_bar(metrics.close_rate_by(folios, "vendedor"), "vendedor")
        st.subheader("Por qué se pierden")
        lr = metrics.loss_reasons(q)
        fig = px.bar(lr, x="parte", y="motivo", orientation="h", text=lr["parte"].map(fmt.pct))
        fig.update_traces(marker_color=MUTED, hovertemplate="%{y}: %{x:.0%} del monto perdido")
        fig.update_yaxes(categoryorder="total ascending")
        chart(fig, height=260, pct_x=True)

    st.subheader("Tasa de cierre por mes")
    trend = metrics.close_rate_by_month(folios)
    fig = px.line(trend, x="mes", y="tasa", markers=True)
    fig.update_traces(line_color=PALETTE[0], hovertemplate="%{x|%b %Y}: %{y:.0%}")
    chart(fig, height=260, pct_y=True)
    st.caption(
        "Por mes en que se envió la cotización. Un mes aparece cuando ya se decidió la mayoría de "
        "sus cotizaciones: las ganadas se deciden antes que las perdidas, y un mes a medias "
        "parecería un récord."
    )

    cold = metrics.cold_quotes(folios, ctx.as_of)
    st.subheader(f"Cotizaciones sin respuesta hace más de {metrics.COLD_DAYS} días")
    if cold.empty:
        st.success("Ninguna: todas las cotizaciones abiertas son recientes.")
    else:
        st.caption(
            fmt.md(
                f"{len(cold)} cotizaciones por {fmt.money(cold['importe'].sum())}. Ordenadas por monto."
            )
        )
        show = cold[["folio", "fecha", "cliente", "vendedor", "marca", "importe", "dias"]].rename(
            columns={
                "folio": "Folio",
                "fecha": "Fecha",
                "cliente": "Cliente",
                "vendedor": "Vendedor",
                "marca": "Marca",
                "importe": "Importe",
                "dias": "Días sin respuesta",
            }
        )
        st.dataframe(
            show,
            hide_index=True,
            width="stretch",
            column_config={
                "Fecha": st.column_config.DateColumn(format="DD/MM/YYYY"),
                "Importe": st.column_config.NumberColumn(format="$%,.0f"),
            },
        )
        csv_button(show, "cotizaciones_sin_respuesta.csv")


def _rate_bar(df: pd.DataFrame, key: str, sort: bool = True):
    if df.empty:
        st.caption("Sin cotizaciones decididas.")
        return
    df = df.copy()
    df["texto"] = [
        f"{fmt.pct(t)}  ({g} de {c})"
        for t, g, c in zip(df["tasa"], df["ganadas"], df["cerradas"], strict=True)
    ]
    fig = px.bar(df, x="tasa", y=key, orientation="h", text="texto")
    fig.update_traces(marker_color=PALETTE[0], hovertemplate="%{y}: %{x:.0%}", textposition="auto")
    fig.update_yaxes(
        categoryorder="total ascending" if sort else "trace",
        autorange="reversed" if not sort else True,
        type="category",
    )
    fig.update_xaxes(range=[0, max(0.6, df["tasa"].max() * 1.15)])
    chart(fig, height=60 + 42 * len(df), pct_x=True)


# --- orders ------------------------------------------------------------------------------------


def orders_page(ctx: Context):
    header("Pedidos y entregas", ctx, "Qué se vendió y si llegó cuando se prometió")
    d = ctx.data
    if d.orders.empty:
        no_data("pedidos", "Pedidos")
        return
    orders = ctx.period(d.orders, "fecha_pedido")
    folios_all = metrics.order_folios(d.orders)
    delivered = ctx.period(folios_all, "fecha_entrega")
    s = metrics.sales_summary(orders)
    ds = metrics.delivery_summary(delivered) if len(delivered) else None
    c = st.columns(5)
    has_price = "precio_venta" in d.orders
    if has_price:
        c[0].metric("Ventas", fmt.money(s.sales))
        c[1].metric("Ticket promedio", fmt.money(s.ticket), help="Ventas entre pedidos.")
    c[2].metric(
        "Pedidos",
        fmt.num(s.orders),
        f"{fmt.num(s.pieces)} piezas",
        delta_color="off",
        delta_arrow="off",
    )
    if ds:
        c[3].metric(
            "Entregas a tiempo",
            fmt.pct(ds.on_time),
            f"{ds.delivered} entregados",
            delta_color="off",
            delta_arrow="off",
        )
        c[4].metric(
            "Tiempo real vs prometido",
            f"{fmt.num(ds.actual_days)} vs {fmt.num(ds.promised_days)} días",
            help="Mediana de días del pedido a la entrega, contra los días prometidos.",
        )

    if has_price and len(orders):
        st.subheader("Ventas por mes y marca")
        by = complete_months(
            metrics.monthly(orders, "fecha_pedido", "precio_venta", "marca"), ctx.as_of
        )
        fig = px.bar(
            by, x="mes", y="precio_venta", color="marca", color_discrete_map=colors(by["marca"])
        )
        fig.update_traces(hovertemplate="%{x|%b %Y}: $%{y:,.0f}")
        chart(fig)

    if len(delivered):
        left, right = st.columns(2)
        with left:
            st.subheader("Entregas a tiempo por mes")
            t = complete_months(metrics.on_time_by(delivered, "mes"), ctx.as_of)
            fig = px.bar(t, x="mes", y="tasa", text=t["tasa"].map(fmt.pct))
            fig.update_traces(
                marker_color=[GOOD if v >= 0.8 else WARN if v >= 0.65 else BAD for v in t["tasa"]],
                hovertemplate="%{x|%b %Y}: %{y:.0%}",
            )
            fig.update_yaxes(range=[0, 1.05])
            chart(fig, height=300, pct_y=True)
            st.caption("Verde: 80 % o más. Ámbar: 65 a 80 %. Rojo: menos.")
        with right:
            st.subheader("Por marca")
            t = metrics.on_time_by(delivered, "marca")
            t["texto"] = [
                f"{fmt.pct(v)} · {fmt.days(r)} de retraso"
                for v, r in zip(t["tasa"], t["dias_retraso"], strict=True)
            ]
            fig = px.bar(t, x="tasa", y="marca", orientation="h", text="texto")
            fig.update_traces(marker_color=PALETTE[0], hovertemplate="%{y}: %{x:.0%}")
            fig.update_xaxes(range=[0, 1.05])
            chart(fig, height=300, pct_x=True)
            st.caption("Días de retraso: promedio entre los pedidos que llegaron tarde.")

    if has_price and len(orders):
        st.subheader("Clientes que más compran")
        top = (
            orders.groupby("cliente")
            .agg(ventas=("precio_venta", "sum"), pedidos=("folio_pedido", "nunique"))
            .sort_values("ventas", ascending=False)
            .head(10)
            .reset_index()
        )
        top["parte"] = top["ventas"] / orders["precio_venta"].sum()
        st.caption(fmt.md(f"Los cinco más grandes son el {fmt.pct(s.top5_share)} de las ventas."))
        st.dataframe(
            top.rename(
                columns={
                    "cliente": "Cliente",
                    "ventas": "Ventas",
                    "pedidos": "Pedidos",
                    "parte": "Parte",
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Ventas": st.column_config.NumberColumn(format="$%,.0f"),
                "Parte": st.column_config.ProgressColumn(
                    format="percent", min_value=0, max_value=float(max(top["parte"].max(), 0.01))
                ),
            },
        )


# --- shop floor --------------------------------------------------------------------------------


def shop_page(ctx: Context):
    header("Taller", ctx, "Cuánto tarda cada etapa y dónde se atora el trabajo")
    pr = ctx.data.production
    if pr.empty:
        no_data("producción", "Producción")
        return
    period = ctx.period(pr, "fecha_inicio")
    s = metrics.stage_summary(period, ctx.as_of)
    now = metrics.stage_summary(pr, ctx.as_of)  # work in progress is "now", whatever the period
    c = st.columns(4)
    c[0].metric("Piezas en proceso", fmt.num(now["en_proceso"].sum()))
    slow = s.dropna(subset=["mediana"])
    if len(slow):
        top = slow.loc[slow["mediana"].idxmax()]
        c[1].metric(
            "Etapa más lenta",
            top["etapa"],
            f"{fmt.days(top['mediana'])} típicos",
            delta_color="off",
            delta_arrow="off",
        )
    busiest = now.loc[now["en_proceso"].idxmax()]
    c[2].metric(
        "Donde hay más trabajo",
        busiest["etapa"],
        f"{int(busiest['en_proceso'])} piezas",
        delta_color="off",
        delta_arrow="off",
    )
    c[3].metric("Etapas terminadas", fmt.num(s["terminadas"].sum()))

    left, right = st.columns(2)
    with left:
        st.subheader("Días por etapa")
        long = s.melt(
            id_vars="etapa", value_vars=["mediana", "p80"], var_name="medida", value_name="dias"
        )
        long["medida"] = long["medida"].map(
            {"mediana": "Típico (mediana)", "p80": "Casos lentos (8 de cada 10 tardan menos)"}
        )
        fig = px.bar(
            long,
            x="dias",
            y="etapa",
            color="medida",
            barmode="group",
            orientation="h",
            color_discrete_sequence=[PALETTE[0], "#9cc3dc"],
        )
        fig.update_yaxes(categoryorder="array", categoryarray=list(STAGES)[::-1])
        fig.update_traces(hovertemplate="%{y}: %{x:.0f} días")
        chart(fig, height=330)
    with right:
        st.subheader("Trabajo en cada etapa hoy")
        fig = px.bar(
            now,
            x="en_proceso",
            y="etapa",
            orientation="h",
            text="en_proceso",
            hover_data={"dias_en_proceso": ":.0f"},
        )
        fig.update_traces(marker_color=PALETTE[1], hovertemplate="%{y}: %{x} piezas")
        fig.update_yaxes(categoryorder="array", categoryarray=list(STAGES)[::-1])
        chart(fig, height=330)

    st.subheader("Días típicos por etapa, mes a mes")
    trend = complete_months(metrics.stage_by_month(period), ctx.as_of)
    fig = px.line(
        trend,
        x="mes",
        y="dias",
        color="etapa",
        markers=True,
        color_discrete_map=dict(zip(STAGES, PALETTE, strict=False)),
        category_orders={"etapa": list(STAGES)},
    )
    fig.update_traces(hovertemplate="%{x|%b %Y}: %{y:.0f} días")
    chart(fig, height=320)
    st.caption("Mediana de días de las etapas que empezaron ese mes.")

    st.subheader("Piezas que llevan más tiempo en su etapa")
    open_ = pr[pr["fecha_fin"].isna()].copy()
    if open_.empty:
        st.caption("No hay piezas en proceso.")
        return
    open_["dias"] = (pd.Timestamp(ctx.as_of) - open_["fecha_inicio"]).dt.days
    typical = s.set_index("etapa")["mediana"]
    open_["normal"] = open_["etapa"].map(typical)
    pieces = ctx.data.orders.set_index(["folio_pedido", "partida"])["pieza"]
    open_["pieza"] = [
        pieces.get((f, p), "") for f, p in zip(open_["folio_pedido"], open_["partida"], strict=True)
    ]
    show = open_.sort_values("dias", ascending=False)[
        [
            "folio_pedido",
            "partida",
            "pieza",
            "etapa",
            "responsable",
            "fecha_inicio",
            "dias",
            "normal",
        ]
    ].rename(
        columns={
            "folio_pedido": "Pedido",
            "partida": "Partida",
            "pieza": "Pieza",
            "etapa": "Etapa",
            "responsable": "Responsable",
            "fecha_inicio": "Desde",
            "dias": "Días ahí",
            "normal": "Normal",
        }
    )
    st.dataframe(
        show,
        hide_index=True,
        width="stretch",
        column_config={
            "Desde": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Normal": st.column_config.NumberColumn(format="%.0f"),
        },
    )


# --- risk --------------------------------------------------------------------------------------


def risk_page(ctx: Context):
    st.title("Pedidos en riesgo")
    st.caption(
        fmt.md(
            f"Piezas sin entregar al {fmt.day(ctx.as_of)}, con la fecha en que, al ritmo actual del taller, quedarían listas."
        )
    )
    if ctx.data.orders.empty:
        no_data("pedidos", "Pedidos")
        return
    r = risk_table(ctx)
    if r.empty:
        st.success("Todas las piezas vendidas ya se entregaron.")
        return
    counts = r["estado"].value_counts()
    c = st.columns(4)
    help_ = {
        risk.LATE: "Ya pasó la fecha prometida.",
        risk.AT_RISK: "Al ritmo actual, se termina después de la fecha prometida.",
        risk.TIGHT: f"Llega, pero con menos de {risk.TIGHT_DAYS} días de margen.",
        risk.OK: "Llega con holgura.",
    }
    for col, status in zip(c, risk.STATUS_ORDER, strict=True):
        col.metric(
            f"{STATUS_ICONS[status]} {status}", fmt.num(counts.get(status, 0)), help=help_[status]
        )

    chosen = st.pills(
        "Mostrar",
        list(risk.STATUS_ORDER),
        selection_mode="multi",
        default=[risk.LATE, risk.AT_RISK, risk.TIGHT],
        format_func=lambda s: f"{STATUS_ICONS[s]} {s}",
    )
    view = r[r["estado"].isin(chosen or risk.STATUS_ORDER)].copy()
    view["estado"] = view["estado"].map(lambda s: f"{STATUS_ICONS[s]} {s}")
    show = view[
        [
            "estado",
            "folio_pedido",
            "cliente",
            "pieza",
            "motivo",
            "fecha_prometida",
            "estimada",
            "holgura",
            "etapa_actual",
            "partida",
            "vendedor",
        ]
    ].rename(
        columns={
            "estado": "Estado",
            "folio_pedido": "Pedido",
            "partida": "Partida",
            "cliente": "Cliente",
            "pieza": "Pieza",
            "etapa_actual": "Dónde está",
            "fecha_prometida": "Prometida",
            "estimada": "Estimada",
            "holgura": "Holgura (días)",
            "motivo": "Por qué",
            "vendedor": "Vendedor",
        }
    )
    st.dataframe(
        show,
        hide_index=True,
        width="stretch",
        column_config={
            "Prometida": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Estimada": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Por qué": st.column_config.TextColumn(width="large"),
        },
    )
    csv_button(show, "pedidos_en_riesgo.csv")

    with st.expander("¿Cómo se calcula la fecha estimada?"):
        st.markdown(
            f"Para cada pieza se suman las etapas que le faltan, con lo que cada etapa ha tardado "
            f"normalmente en los últimos {risk.WINDOW_DAYS} días (la mediana), más la espera usual entre "
            "etapas y antes de entregar. Si una pieza ya lleva en su etapa más de lo normal, se le "
            "da un margen mínimo en lugar de suponer que termina hoy. Las etapas que le faltan son las "
            "que esa misma pieza siguió antes: una mesa no pasa por tapicería."
        )
        bt = _backtest(ctx.data.orders, ctx.data.production, ctx.as_of, _signature(ctx))
        if bt:
            st.markdown(
                fmt.md(
                    f"**Comprobado contra el historial de estos datos:** se repitió el cálculo como si fuera "
                    f"el día 15 de cada uno de los últimos 12 meses, con solo lo que se sabía ese día "
                    f"({fmt.num(bt['piezas'])} piezas). De las piezas que marcó como «va tarde», "
                    f"**{fmt.pct(bt['precision'])}** sí llegaron tarde; de las que llegaron tarde, avisó "
                    f"de **{fmt.pct(bt['recall'])}**. Sin el estimador, adivinar daría "
                    f"{fmt.pct(bt['base'])}. La fecha estimada falló por {fmt.days(bt['error'])} "
                    "(mediana)."
                )
            )


# --- margin ------------------------------------------------------------------------------------


def margin_page(ctx: Context):
    header("Margen", ctx, "Cuánto queda de cada venta después de lo que costó hacerla")
    d = ctx.data
    if d.costs.empty:
        no_data("costos", "Costos")
        return
    lines = metrics.line_margin(ctx.period(d.orders, "fecha_entrega"), d.costs)
    lines = lines.dropna(subset=["fecha_entrega"])
    missing = lines[lines["costo"].isna()]
    total = metrics.margin_by(lines.assign(todo="t"), "todo")
    if total.empty:
        st.info("Ningún pedido entregado en el periodo tiene costos cargados.")
        return
    t = total.iloc[0]
    c = st.columns(4)
    c[0].metric("Margen bruto", fmt.pct(t["margen_pct"]))
    c[1].metric("Margen en pesos", fmt.money(t["margen"]))
    c[2].metric("Ventas con costo", fmt.money(t["ventas"]))
    c[3].metric(
        "Piezas sin costos",
        fmt.num(len(missing)),
        help="Piezas entregadas sin ningún costo cargado. No entran al margen: con costo cero "
        "parecería que se ganó todo.",
    )
    st.caption(
        "Margen de lo entregado en el periodo: hasta la entrega no están todos los costos (el flete, por ejemplo)."
    )

    left, right = st.columns(2)
    with left:
        st.subheader("Por marca")
        by = metrics.margin_by(lines, "marca")
        fig = px.bar(
            by,
            x="marca",
            y="margen_pct",
            text=by["margen_pct"].map(fmt.pct),
            color="marca",
            color_discrete_map=colors(by["marca"]),
        )
        fig.update_traces(hovertemplate="%{x}: %{y:.0%}")
        fig.update_layout(showlegend=False)
        chart(fig, height=300, pct_y=True)
    with right:
        st.subheader("Por vendedor")
        by = metrics.margin_by(lines, "vendedor").sort_values("margen_pct")
        fig = px.bar(
            by, x="margen_pct", y="vendedor", orientation="h", text=by["margen_pct"].map(fmt.pct)
        )
        fig.update_traces(marker_color=PALETTE[0], hovertemplate="%{y}: %{x:.0%}")
        chart(fig, height=300, pct_x=True)
        st.caption("Un margen menor con la misma marca suele ser descuento.")

    st.subheader("Margen por mes y marca")
    all_lines = metrics.line_margin(d.orders, d.costs).dropna(subset=["fecha_entrega"])
    by_month = (
        all_lines.dropna(subset=["costo"])
        .assign(mes=lambda x: x["fecha_entrega"].dt.to_period("M").dt.to_timestamp())
        .groupby(["mes", "marca"])
        .agg(ventas=("precio_venta", "sum"), costo=("costo", "sum"))
        .reset_index()
    )
    by_month["margen_pct"] = 1 - by_month["costo"] / by_month["ventas"]
    by_month = complete_months(by_month, ctx.as_of)
    fig = px.line(
        by_month,
        x="mes",
        y="margen_pct",
        color="marca",
        markers=True,
        color_discrete_map=colors(by_month["marca"]),
    )
    fig.update_traces(hovertemplate="%{x|%b %Y}: %{y:.0%}")
    chart(fig, height=300, pct_y=True)
    st.caption("Todos los meses disponibles, no solo el periodo elegido, para ver la tendencia.")

    left, right = st.columns(2)
    with left:
        st.subheader("A dónde se va el dinero")
        mix = metrics.cost_mix(ctx.period(d.orders, "fecha_entrega"), d.costs)
        fig = px.bar(mix, x="marca", y="parte", color="concepto", color_discrete_sequence=PALETTE)
        fig.update_traces(hovertemplate="%{x} · %{fullData.name}: %{y:.0%} del precio")
        chart(fig, height=320, pct_y=True)
        st.caption("Cada costo como parte del precio de venta.")
    with right:
        st.subheader("Piezas con menos margen")
        pieces = metrics.margin_by(lines, "pieza").sort_values("margen_pct")
        pieces = pieces[pieces["piezas"] >= 3].head(8)
        st.dataframe(
            pieces[["pieza", "piezas", "ventas", "margen_pct"]].rename(
                columns={
                    "pieza": "Pieza",
                    "piezas": "Unidades",
                    "ventas": "Ventas",
                    "margen_pct": "Margen",
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Ventas": st.column_config.NumberColumn(format="$%,.0f"),
                "Margen": st.column_config.NumberColumn(format="percent"),
            },
        )
    if len(missing):
        with st.expander(f"Piezas entregadas sin costos ({len(missing)})"):
            st.dataframe(
                missing[["folio_pedido", "partida", "pieza", "fecha_entrega", "precio_venta"]],
                hide_index=True,
                width="stretch",
            )


# --- billing -----------------------------------------------------------------------------------


def billing_page(ctx: Context):
    header("Facturación", ctx, "Lo facturado ante el SAT contra lo entregado")
    d = ctx.data
    if d.invoices.empty:
        no_data("facturas", None)
        st.caption(
            "Sube los XML de tus facturas, o el ZIP de la descarga masiva del SAT, en «Cargar datos»."
        )
        return
    inv = ctx.period(d.invoices, "fecha")
    matched, orphan = metrics.match_invoices(d.orders, d.invoices)
    pending = metrics.to_invoice(matched, ctx.as_of)
    mismatch = metrics.amount_mismatches(matched)
    billed = float(np.where(inv["tipo"] == "E", -inv["subtotal_mxn"], inv["subtotal_mxn"]).sum())
    c = st.columns(4)
    c[0].metric(
        "Facturado",
        fmt.money(billed),
        f"{len(inv)} facturas",
        delta_color="off",
        delta_arrow="off",
        help="Subtotal sin IVA; notas de crédito restan.",
    )
    c[1].metric(
        "Entregado sin factura",
        fmt.money(pending["precio_venta"].sum()),
        f"{len(pending)} pedidos",
        delta_color="off",
        delta_arrow="off",
    )
    c[2].metric(
        "Facturas sin pedido", fmt.num(len(orphan)), help="Facturas que ningún pedido menciona."
    )
    c[3].metric(
        "Montos que no cuadran",
        fmt.num(len(mismatch)),
        help="Pedidos cuya factura difiere más de 1 % del precio.",
    )

    st.subheader("Facturado por mes")
    by = complete_months(metrics.billing_by_month(d.invoices), ctx.as_of)
    by = by[(by["mes"] >= ctx.start.to_period("M").to_timestamp())]
    fig = px.bar(by, x="mes", y="neto")
    fig.update_traces(marker_color=PALETTE[0], hovertemplate="%{x|%b %Y}: $%{y:,.0f}")
    chart(fig, height=280)

    st.subheader("Pedidos entregados sin factura")
    if pending.empty:
        st.success("Todo lo entregado está facturado.")
    else:
        show = pending[
            ["folio_pedido", "cliente", "fecha_entrega", "dias_desde_entrega", "precio_venta"]
        ].rename(
            columns={
                "folio_pedido": "Pedido",
                "cliente": "Cliente",
                "fecha_entrega": "Entregado",
                "dias_desde_entrega": "Días desde la entrega",
                "precio_venta": "Importe",
            }
        )
        st.dataframe(
            show,
            hide_index=True,
            width="stretch",
            column_config={
                "Entregado": st.column_config.DateColumn(format="DD/MM/YYYY"),
                "Importe": st.column_config.NumberColumn(format="$%,.0f"),
            },
        )
        csv_button(show, "entregado_sin_factura.csv")
    if len(orphan):
        with st.expander(f"Facturas sin pedido ({len(orphan)})"):
            st.dataframe(
                orphan[["serie", "folio", "fecha", "receptor_nombre", "subtotal_mxn", "uuid"]],
                hide_index=True,
                width="stretch",
            )
    if len(mismatch):
        with st.expander(f"Montos que no cuadran ({len(mismatch)})"):
            st.dataframe(
                mismatch[["folio_pedido", "factura", "precio_venta", "facturado", "diferencia"]],
                hide_index=True,
                width="stretch",
            )


# --- upload ------------------------------------------------------------------------------------


@st.cache_data(show_spinner=False)
def _example_zip(today) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in sample.files(sample.generate(today), typos=False):
            zf.writestr(name, content)
    return buf.getvalue()


@st.cache_data(show_spinner=False)
def _blank(name: str) -> bytes:
    return workbook_bytes(BY_NAME[name])


def upload_page(ctx: Context):
    st.title("Cargar datos")
    role = ctx.role
    allowed = set(role.uploads)
    st.markdown(
        "1. Descarga la plantilla de lo que quieras cargar y llénala (o pega ahí lo que exportes de tu sistema).\n"
        "2. Súbela aquí. Puedes subir varias a la vez; el sistema reconoce cuál es cuál.\n"
        "3. Revisa el reporte: las filas con error no se cargan y te dice en qué fila está cada problema.\n"
        "4. Cargar una plantilla otra vez reemplaza la anterior; las demás se quedan igual."
    )
    st.subheader("Plantillas")
    cols = st.columns(len(TEMPLATES) + 1)
    for col, t in zip(cols, TEMPLATES, strict=False):
        col.download_button(
            t.label,
            _blank(t.name),
            file_name=f"plantilla_{t.name}.xlsx",
            icon=":material/download:",
            disabled=t.name not in allowed,
            help=t.purpose,
            width="stretch",
        )
    cols[-1].download_button(
        "Ejemplo lleno (ZIP)",
        _example_zip(ctx.as_of),
        file_name="ejemplo_taller.zip",
        icon=":material/folder_zip:",
        help="Las cuatro plantillas llenas con los datos de la demostración y las facturas XML.",
        width="stretch",
    )
    if "facturas" in allowed:
        st.caption(
            "Facturas: sube los XML tal como los da el SAT, sueltos o en el ZIP de la descarga masiva. No llevan plantilla."
        )

    st.subheader("Subir archivos")
    files = st.file_uploader(
        "Arrastra aquí tus archivos",
        type=["xlsx", "csv", "xml", "zip"],
        accept_multiple_files=True,
        help="Excel (.xlsx), CSV, XML de facturas o ZIP con XML.",
    )
    current, demo = current_dataset()
    if files:
        payload = [(f.name, f.getvalue()) for f in files]
        groups = dataset.classify(payload)
        refused = {k: v for k, v in groups.items() if k not in allowed and k != "?"}
        for table, items in refused.items():
            st.error(
                f"{', '.join(n for n, _ in items)}: {role.label} no puede cargar "
                f"{dataset.table_label(table).lower()}.",
                icon=":material/block:",
            )
        ok = [(n, c) for k, v in groups.items() if k in allowed or k == "?" for n, c in v]
        if ok:
            preview = current.with_files(ok)
            names = {n for n, _ in ok}
            usable = _report(current, preview, names)
            if any(r.rows_loaded for r in usable) and st.button(
                "Usar estos datos", type="primary", icon=":material/check:"
            ):
                st.session_state["uploaded"] = preview
                st.toast("Datos cargados. Los tableros ya los usan.", icon="✅")
                st.rerun()
    if "uploaded" in st.session_state:
        st.divider()
        st.caption("Estás viendo datos que cargaste en esta sesión.")
        if demo and st.button("Volver a los datos de demostración", icon=":material/restart_alt:"):
            del st.session_state["uploaded"]
            st.rerun()

    st.subheader("Lo que hay cargado ahora")
    _loaded(current)


def _report(current: dataset.Dataset, preview: dataset.Dataset, names: set[str]) -> list:
    """What these files did: their rows, and only the problems they brought (not the ones the
    data already had)."""
    invoices = any(n.lower().endswith((".xml", ".zip")) for n in names)
    rows = [r for r in preview.report if r.file in names or (invoices and r.table == "Facturas")]
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Archivo": r.file,
                    "Plantilla": r.table,
                    "Filas leídas": r.rows_read,
                    "Cargadas": r.rows_loaded,
                    "Con error": r.rows_rejected,
                }
                for r in rows
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    known = set(current.issues)
    issues = [i for i in preview.issues if i not in known]
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity != "error"]
    if not issues:
        st.success("Todo se leyó sin problemas.", icon=":material/check_circle:")
    if errors:
        st.error(
            f"{plural(len(errors), 'fila', 'filas')} con error no se "
            f"{'cargó' if len(errors) == 1 else 'cargaron'}. Corrígelas en el archivo y vuelve a "
            "subirlo.",
            icon=":material/error:",
        )
        st.dataframe(_issues(errors), hide_index=True, width="stretch")
    if warnings:
        st.warning(
            f"{plural(len(warnings), 'aviso', 'avisos')}: se cargó, pero revisa.",
            icon=":material/warning:",
        )
        st.dataframe(_issues(warnings), hide_index=True, width="stretch")
    return rows


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _issues(items) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Archivo": i.file,
                "Fila": str(i.row) if i.row else "",
                "Columna": i.column,
                "Problema": i.message,
            }
            for i in items
        ]
    )


def _loaded(d: dataset.Dataset):
    rows = []
    for label, df, col in (
        ("Cotizaciones", d.quotes, "fecha"),
        ("Pedidos", d.orders, "fecha_pedido"),
        ("Producción", d.production, "fecha_inicio"),
        ("Costos", d.costs, "fecha"),
        ("Facturas", d.invoices, "fecha"),
    ):
        dates = df[col].dropna() if len(df) and col in df else pd.Series(dtype="datetime64[ns]")
        rows.append(
            {
                "Datos": label,
                "Filas": len(df),
                "Desde": fmt.day(dates.min()) if len(dates) else "—",
                "Hasta": fmt.day(dates.max()) if len(dates) else "—",
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


PAGES = {
    roles.SUMMARY: summary,
    roles.QUOTES: quotes_page,
    roles.ORDERS: orders_page,
    roles.SHOP: shop_page,
    roles.RISK: risk_page,
    roles.MARGIN: margin_page,
    roles.BILLING: billing_page,
    roles.UPLOAD: upload_page,
}
