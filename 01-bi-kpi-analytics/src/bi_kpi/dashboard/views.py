"""The dashboard's pages, sidebar and cached queries; app.py only calls ``main()``.

Five pages over the KPI engine. The user picker at the top of the sidebar exists to show
row-level security: switching to a regional manager or a seller changes every number, because
every query goes through bi_kpi.security.
"""

from __future__ import annotations

from contextlib import contextmanager

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from bi_kpi import kpis, security
from bi_kpi.config import load_settings
from bi_kpi.dashboard import data, text
from bi_kpi.dashboard.data import View

BLUE, ORANGE, RED, GREEN, GRAY = "#2563eb", "#f59e0b", "#dc2626", "#16a34a", "#94a3b8"
ROLE_LABELS = {
    "director": "Dirección — ve todo",
    "analyst": "Analista — todo, sin datos individuales de clientes",
    "regional_manager": "Gerente regional — solo clientes de su región",
    "seller": "Vendedor — solo sus propias ventas",
}
SEVERITY_LABELS = {
    "quarantine": "🔴 Cuarentena",
    "fixed": "🟢 Corregido",
    "warning": "🟠 Advertencia",
    "info": "🔵 Informativo",
}


@st.cache_resource
def _resources():
    settings = load_settings()
    catalog = kpis.load_kpis(settings.config_dir / "kpis.yaml")
    users = security.load_users(settings.config_dir / "users.yaml")
    return settings, catalog, users


SETTINGS, CATALOG, USERS = _resources()
_LOADED = {"mtime": None}


@contextmanager
def db():
    """A read-only connection held only for one query: `bi load` and `bi run-daily` can write
    the warehouse while the dashboard is open (Windows won't open a file another process
    holds)."""
    con = data.connect(SETTINGS.warehouse)
    try:
        yield con
    finally:
        con.close()


def refresh_if_reloaded() -> None:
    """New data since the last run (a `bi load`): drop every cached result."""
    mtime = SETTINGS.warehouse.stat().st_mtime
    if _LOADED["mtime"] != mtime:
        st.cache_data.clear()
        _LOADED["mtime"] = mtime


def synthetic() -> bool:
    return (SETTINGS.raw_dir / "SYNTHETIC_DATA.txt").exists()


# --- cached queries (keyed by the View, which includes the user) ------------------------------


@st.cache_data(show_spinner=False)
def kpi_values(view: View) -> pd.DataFrame:
    with db() as con:
        return data.kpi_values(con, view, CATALOG)


@st.cache_data(show_spinner=False)
def kpi_by(view: View, ids: tuple[str, ...], by: str) -> pd.DataFrame:
    with db() as con:
        return data.kpi_by(con, view, CATALOG, list(ids), by)


@st.cache_data(show_spinner=False)
def plan_by_month(view: View) -> pd.DataFrame:
    with db() as con:
        return data.plan_by_month(con, view)


@st.cache_data(show_spinner=False)
def state_map(view: View) -> pd.DataFrame:
    with db() as con:
        return data.state_map(con, view, CATALOG)


@st.cache_data(show_spinner=False)
def installments(view: View) -> pd.DataFrame:
    with db() as con:
        return data.installments(con, view)


@st.cache_data(show_spinner=False)
def delay_vs_score(view: View):
    with db() as con:
        return data.delay_vs_score(con, view)


@st.cache_data(show_spinner=False)
def alerts(view: View) -> pd.DataFrame:
    with db() as con:
        return data.alerts(con, view, CATALOG)


@st.cache_data(show_spinner=False)
def target_status(view: View, by: str) -> pd.DataFrame:
    with db() as con:
        return data.target_status(con, view, CATALOG, by)


@st.cache_data(show_spinner=False)
def anomaly_alerts(view: View) -> pd.DataFrame:
    with db() as con:
        return data.anomaly_alerts(con, view)


@st.cache_data(show_spinner=False)
def anomaly_series(view: View, series: str, region: str) -> pd.DataFrame:
    with db() as con:
        return data.anomaly_series(con, view, series, region)


@st.cache_data(show_spinner=False)
def event_calendar() -> pd.DataFrame:
    with db() as con:
        return data.event_calendar(con)


@st.cache_data(show_spinner=False)
def event_impact(user: security.User) -> pd.DataFrame:
    with db() as con:
        return data.event_impact(con, user)


@st.cache_data(show_spinner=False)
def alert_regions(user: security.User) -> list[str]:
    with db() as con:
        return data.alert_regions_for(con, user)


@st.cache_data(show_spinner=False)
def options(user: security.User) -> dict:
    with db() as con:
        return data.options(con, View(user))


@st.cache_data(show_spinner=False)
def date_bounds():
    with db() as con:
        return data.date_bounds(con)


@st.cache_data(show_spinner=False)
def black_friday_months() -> list[int]:
    with db() as con:
        rows = con.execute(
            "SELECT DISTINCT year_month FROM mart.dim_date WHERE is_black_friday"
        ).fetchall()
    return [ym for (ym,) in rows]


# --- sidebar --------------------------------------------------------------------------------


def sidebar() -> View:
    st.sidebar.markdown("### 📊 BI & KPIs · Olist")
    names = list(USERS.by_name)
    # ?usuario=gerente.sudeste preselects a user (demo links, screenshots). It only picks the
    # starting option of a demo picker; an unknown name falls back to the first user.
    wanted = st.query_params.get("usuario")
    name = st.sidebar.selectbox(
        "Usuario",
        names,
        index=names.index(wanted) if wanted in names else 0,
        help="Demostración de seguridad por rol: cada usuario ve solo lo que le corresponde.",
    )
    user = USERS.get(name)
    st.sidebar.caption(ROLE_LABELS[user.role])

    first, last, c0, c1 = date_bounds()
    picked = st.sidebar.date_input(
        "Fechas de compra",
        (c0, c1),
        min_value=first,
        max_value=last,
        format="DD/MM/YYYY",
        help="Por defecto, los meses completos (el dataset arranca y termina a medias).",
    )
    start, end = (picked[0], picked[1]) if len(picked) == 2 else (picked[0], picked[0])

    opts = options(user)
    filters = []
    region = st.sidebar.selectbox("Región del cliente", ["Todas", *opts["regions"]])
    if region != "Todas":
        filters.append(("region", region))
    states = opts["states_by_region"][region] if region != "Todas" else opts["states"]
    state = st.sidebar.selectbox("Estado", ["Todos", *states])
    if state != "Todos":
        filters.append(("state", state))
    category = st.sidebar.selectbox("Categoría", ["Todas", *opts["categories"]])
    if category != "Todas":
        filters.append(("category", category))
    currency = st.sidebar.radio("Moneda", list(kpis.CURRENCIES), horizontal=True)

    st.sidebar.divider()
    st.sidebar.caption(
        "Datos reales y públicos de **Olist** (marketplace brasileño, 2016–2018), "
        "CC BY-NC-SA 4.0. Tipo de cambio del día de compra (BCE)."
    )
    return View(user=user, start=start, end=end, filters=tuple(filters), currency=currency)


# --- shared pieces --------------------------------------------------------------------------


def md(s: str) -> str:
    """Markdown-safe: Streamlit reads text between two "$" as a formula ("R$ 1 ... R$ 2")."""
    return s.replace("$", r"\$")


def values_of(view: View) -> dict[str, float]:
    df = kpi_values(view)
    return dict(zip(df["kpi"], df["value"], strict=True))


def metric(col, kpi_id: str, values: dict[str, float], currency: str, caption: str = ""):
    k = CATALOG[kpi_id]
    value = values.get(kpi_id)
    delta, color = None, "off"
    if k.target is not None and value == value and value is not None:
        gap = value - k.target
        if k.unit == "ratio":
            delta = f"{gap * 100:+.1f} pp vs meta {k.target:.0%}"
        else:
            delta = f"{gap:+.2f} vs meta {k.target:g}"
        color = "normal" if k.direction == "up" else "inverse"
    col.metric(k.name, text.fmt(value, k.unit, currency), delta, delta_color=color)
    if caption:
        col.caption(md(caption))


def month_axis(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["mes"] = pd.to_datetime(df["key"].astype(int).astype(str), format="%Y%m")
    return df


def chart(fig: go.Figure, height: int = 340) -> None:
    fig.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=60, b=10),
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1),
    )
    st.plotly_chart(fig, width="stretch")


def banner(view: View) -> None:
    if synthetic():
        st.warning("Datos **sintéticos** con el esquema de Olist (`bi sample`), no los reales.")
    if view.user.role != "director":
        st.info(f"Viendo como **{view.user.name}** · {ROLE_LABELS[view.user.role]}", icon="🔒")


# --- page 1: executive summary --------------------------------------------------------------


def page_summary(view: View) -> None:
    st.title("Resumen ejecutivo")
    banner(view)
    v = values_of(view)
    c = st.columns(5)
    metric(c[0], "gmv", v, view.currency, "Ventas sin cancelaciones")
    metric(c[1], "plan_attainment", v, view.currency, "Ventas ÷ meta prorrateada")
    metric(
        c[2],
        "orders",
        v,
        view.currency,
        f"Ticket promedio {text.money(v.get('avg_ticket'), view.currency, compact=False)}",
    )
    metric(c[3], "on_time_delivery", v, view.currency, "Entregados antes de la fecha prometida")
    metric(c[4], "avg_score", v, view.currency, "Última reseña de cada pedido")

    monthly = month_axis(kpi_by(view, ("gmv", "orders", "on_time_delivery", "avg_score"), "month"))
    plan = plan_by_month(view)
    fig = go.Figure()
    fig.add_bar(x=monthly["mes"], y=monthly["gmv"], name="Ventas", marker_color=BLUE)
    if len(plan):
        plan = month_axis(plan)
        fig.add_scatter(
            x=plan["mes"],
            y=plan["plan"],
            name="Meta",
            mode="lines+markers",
            line=dict(color=ORANGE, width=3),
        )
    for ym in black_friday_months():
        x = pd.to_datetime(str(ym), format="%Y%m")
        if len(monthly) and monthly["mes"].min() <= x <= monthly["mes"].max():
            fig.add_annotation(x=x, y=1.0, yref="paper", text="Black Friday", showarrow=False)
    fig.update_layout(title=f"Ventas mensuales vs. meta ({view.currency})")
    chart(fig)

    left, right = st.columns([3, 2])
    by_region = kpi_by(view, ("gmv", "on_time_delivery", "plan_attainment"), "region")
    with left:
        # One region visible (a regional manager, or a region filter): break it into states.
        one_region = len(by_region.dropna(subset=["gmv"])) <= 1
        df = kpi_by(view, ("gmv", "on_time_delivery"), "state") if one_region else by_region
        df = df.dropna(subset=["gmv"]).sort_values("gmv")
        fig = px.bar(
            df,
            x="gmv",
            y="key",
            orientation="h",
            color="on_time_delivery",
            color_continuous_scale=[RED, ORANGE, GREEN],
            range_color=(0.8, 1.0),
            labels={"gmv": f"Ventas ({view.currency})", "key": "", "on_time_delivery": "OTD"},
            title=f"Ventas por {'estado' if one_region else 'región'} (color: entregas a tiempo)",
        )
        chart(fig, 300)
    with right:
        st.markdown("##### Fuera de meta")
        a = alerts(view)
        if a.empty:
            st.success("Todos los KPIs con meta la cumplen en la selección.")
        else:
            # Furthest from target in relative terms: 0.25 stars and 9 points are not comparable.
            a = a.assign(rel=(a["gap"] / a["target"]).abs())
            for _, r in a.sort_values("rel", ascending=False).head(6).iterrows():
                where = text.month_label(r["key"]) if r["scope"] == "Mes" else r["key"]
                value = text.fmt(r["value"], r["unit"], view.currency)
                target = text.fmt(r["target"], r["unit"], view.currency)
                st.markdown(md(f"- **{r['name']}** · {where}: {value} (meta {target})"))
            st.caption(f"{len(a)} en total — ver la página Alertas.")

    st.markdown("##### 📝 Lectura")
    sentences = text.narrative(v, by_region, monthly, view.currency)
    st.markdown(md(" ".join(sentences)) if sentences else "Sin datos en la selección.")


# --- page 2: sales --------------------------------------------------------------------------


def page_sales(view: View) -> None:
    st.title("Ventas")
    banner(view)
    monthly = month_axis(kpi_by(view, ("gmv", "orders", "avg_ticket"), "month"))
    fig = go.Figure()
    fig.add_bar(x=monthly["mes"], y=monthly["gmv"], name="Ventas", marker_color=BLUE)
    fig.add_scatter(
        x=monthly["mes"],
        y=monthly["orders"],
        name="Pedidos",
        yaxis="y2",
        line=dict(color=ORANGE, width=3),
    )
    fig.update_layout(
        title="Tendencia mensual",
        yaxis=dict(title=f"Ventas ({view.currency})"),
        yaxis2=dict(title="Pedidos", overlaying="y", side="right", showgrid=False),
    )
    chart(fig)

    left, right = st.columns(2)
    with left:
        m = state_map(view)
        if m["lat"].notna().any():
            fig = px.scatter_geo(
                m,
                lat="lat",
                lon="lng",
                size="gmv",
                color="on_time_delivery",
                hover_name="state_name",
                color_continuous_scale=[RED, ORANGE, GREEN],
                range_color=(0.8, 1.0),
                size_max=45,
                labels={"gmv": "Ventas", "on_time_delivery": "OTD"},
                title="Ventas por estado del cliente (tamaño) y puntualidad (color)",
            )
            fig.update_geos(
                projection_type="mercator",
                lataxis_range=[-34, 6],
                lonaxis_range=[-75, -33],
                showcountries=True,
                countrycolor=GRAY,
                showland=True,
                landcolor="#f8fafc",
                showocean=True,
                oceancolor="#eff6ff",
            )
            chart(fig, 420)
        else:
            fig = px.bar(
                m.sort_values("gmv"),
                x="gmv",
                y="state",
                orientation="h",
                title="Ventas por estado del cliente",
            )
            chart(fig, 420)
    with right:
        cats = kpi_by(view, ("gmv", "avg_ticket", "freight_share"), "category")
        cats = cats.dropna(subset=["gmv"]).nlargest(15, "gmv").sort_values("gmv")
        fig = px.bar(
            cats,
            x="gmv",
            y="key",
            orientation="h",
            hover_data={"avg_ticket": ":,.2f", "freight_share": ":.1%"},
            labels={
                "gmv": f"Ventas ({view.currency})",
                "key": "",
                "avg_ticket": "Ticket",
                "freight_share": "Flete %",
            },
            title="Top 15 categorías",
            color_discrete_sequence=[BLUE],
        )
        chart(fig, 420)

    left, mid, right = st.columns([2, 1, 1])
    with left:
        st.markdown("##### Top 10 vendedores")
        sellers = kpi_by(view, ("gmv", "avg_ticket", "freight_share"), "seller")
        sellers = sellers.dropna(subset=["gmv"]).nlargest(10, "gmv")
        st.dataframe(
            pd.DataFrame(
                {
                    "Vendedor": sellers["key"].str[:8] + "…",
                    "Ventas": sellers["gmv"].map(lambda x: text.money(x, view.currency)),
                    "Ticket": sellers["avg_ticket"].map(
                        lambda x: text.money(x, view.currency, compact=False)
                    ),
                    "Flete %": sellers["freight_share"].map(lambda x: f"{x:.1%}"),
                }
            ),
            hide_index=True,
            width="stretch",
        )
    with mid:
        pay = kpi_by(view, ("gmv",), "payment_type").dropna()
        labels = {
            "credit_card": "Tarjeta",
            "boleto": "Boleto",
            "voucher": "Vale",
            "debit_card": "Débito",
            "not_defined": "Sin definir",
        }
        pay["key"] = pay["key"].map(lambda k: labels.get(k, k))
        fig = px.pie(pay, names="key", values="gmv", hole=0.5, title="Forma de pago")
        fig.update_traces(textinfo="label+percent", textposition="inside")
        fig.update_layout(showlegend=False)
        chart(fig, 300)
    with right:
        inst = installments(view)
        fig = px.bar(
            inst,
            x="installments",
            y="orders",
            title="Mensualidades",
            labels={"installments": "Meses", "orders": "Pedidos"},
            color_discrete_sequence=[BLUE],
        )
        chart(fig, 300)


# --- page 3: operations and satisfaction ----------------------------------------------------


def page_operations(view: View) -> None:
    st.title("Operación y satisfacción")
    banner(view)
    v = values_of(view)
    c = st.columns(5)
    metric(c[0], "on_time_delivery", v, view.currency)
    metric(c[1], "delivery_days", v, view.currency)
    metric(c[2], "freight_share", v, view.currency)
    metric(c[3], "cancel_rate", v, view.currency)
    metric(c[4], "negative_reviews", v, view.currency)

    ids = (
        "on_time_delivery",
        "delivery_days",
        "avg_score",
        "negative_reviews",
        "cancel_rate",
        "freight_share",
    )
    monthly = month_axis(kpi_by(view, ids, "month"))
    left, right = st.columns(2)
    with left:
        fig = go.Figure()
        fig.add_scatter(
            x=monthly["mes"],
            y=monthly["on_time_delivery"],
            name="OTD",
            line=dict(color=BLUE, width=3),
        )
        fig.add_hline(y=0.90, line_dash="dash", line_color=GRAY, annotation_text="meta 90 %")
        fig.add_bar(
            x=monthly["mes"],
            y=monthly["delivery_days"],
            name="Días de entrega",
            yaxis="y2",
            marker_color="rgba(148,163,184,0.45)",
        )
        fig.update_layout(
            title="Entregas a tiempo y días de entrega",
            yaxis=dict(tickformat=".0%", title="OTD"),
            yaxis2=dict(
                title="Días",
                overlaying="y",
                side="right",
                showgrid=False,
                tickformat=".0f",
                rangemode="tozero",
            ),
        )
        chart(fig)
    with right:
        fig = go.Figure()
        fig.add_scatter(
            x=monthly["mes"],
            y=monthly["avg_score"],
            name="Calificación",
            line=dict(color=GREEN, width=3),
        )
        fig.add_hline(y=4.0, line_dash="dash", line_color=GRAY, annotation_text="meta 4.0")
        fig.add_scatter(
            x=monthly["mes"],
            y=monthly["negative_reviews"],
            name="% negativas",
            yaxis="y2",
            line=dict(color=RED, width=2, dash="dot"),
        )
        fig.update_layout(
            title="Calificación y reseñas negativas",
            yaxis=dict(title="Calificación (1–5)"),
            yaxis2=dict(
                title="% negativas", tickformat=".0%", overlaying="y", side="right", showgrid=False
            ),
        )
        chart(fig)

    left, right = st.columns(2)
    with left:
        reg = kpi_by(view, ("on_time_delivery", "delivery_days", "avg_score"), "region")
        reg = reg.dropna(subset=["on_time_delivery"]).sort_values("on_time_delivery")
        fig = px.bar(
            reg,
            x="on_time_delivery",
            y="key",
            orientation="h",
            text=reg["on_time_delivery"].map(lambda x: f"{x:.1%}"),
            hover_data={"delivery_days": ":.1f", "avg_score": ":.2f"},
            labels={
                "on_time_delivery": "OTD",
                "key": "",
                "delivery_days": "Días",
                "avg_score": "Calificación",
            },
            title="Entregas a tiempo por región del cliente",
            color_discrete_sequence=[BLUE],
        )
        fig.add_vline(x=0.90, line_dash="dash", line_color=GRAY)
        fig.update_xaxes(tickformat=".0%", range=[0.7, 1.0])
        chart(fig, 300)
    with right:
        by_day = delay_vs_score(view)
        fig = go.Figure()
        fig.add_bar(
            x=by_day["days_late"],
            y=by_day["score"],
            marker_color=[
                GREEN if d == 0 else ORANGE if d <= 5 else RED for d in by_day["days_late"]
            ],
            customdata=by_day["reviews"],
            hovertemplate="%{x} días tarde: %{y:.2f} ★ (%{customdata:,} reseñas)<extra></extra>",
        )
        fig.update_layout(
            title="Retraso vs. calificación",
            xaxis=dict(title="Días de retraso (0 = a tiempo, 15 = 15 o más)", dtick=1),
            yaxis=dict(title="Calificación promedio", range=[1, 5]),
        )
        chart(fig, 300)
        d = data.delay_summary(by_day)
        if d["on_time"] == d["on_time"] and d["one_day"] == d["one_day"]:
            st.caption(
                f"A tiempo: **{d['on_time']:.2f} ★**. Un solo día tarde: **{d['one_day']:.2f} ★**. "
                f"Una semana o más: **{d['week_plus']:.2f} ★**. El castigo llega con el primer "
                "día de retraso, no se reparte de a poco."
            )

    left, right = st.columns(2)
    with left:
        fig = px.line(
            monthly,
            x="mes",
            y="freight_share",
            title="Flete como % de la venta",
            labels={"freight_share": "Flete %", "mes": ""},
        )
        fig.add_hline(y=0.20, line_dash="dash", line_color=GRAY, annotation_text="máx. 20 %")
        fig.update_yaxes(tickformat=".0%")
        chart(fig, 260)
    with right:
        fig = px.line(
            monthly,
            x="mes",
            y="cancel_rate",
            title="Tasa de cancelación",
            labels={"cancel_rate": "Cancelación", "mes": ""},
        )
        fig.add_hline(y=0.02, line_dash="dash", line_color=GRAY, annotation_text="máx. 2 %")
        fig.update_yaxes(tickformat=".1%")
        chart(fig, 260)


# --- page 4: alerts -------------------------------------------------------------------------


def page_alerts(view: View) -> None:
    st.title("Alertas")
    banner(view)
    anomalies_tab, events_tab, targets_tab = st.tabs(["Anomalías", "Eventos", "Contra la meta"])
    with anomalies_tab:
        anomaly_section(view)
    with events_tab:
        events_section(view)
    with targets_tab:
        target_section(view)


SERIES_LABELS = {
    "carrier_pickups": "⏱ Despachos a paquetería (alerta temprana)",
    "late_dispatch": "⏱ Despachos atrasados (alerta temprana)",
    "orders": "Pedidos (por fecha de compra)",
    "gmv": "Ventas (por semana de compra)",
    "on_time_delivery": "Entregas a tiempo (por fecha en que debían llegar)",
    "negative_reviews": "Reseñas negativas (por semana de la reseña)",
}


def anomaly_section(view: View) -> None:
    st.caption(
        "Cada serie contra su propio pasado: mediana y MAD robusta de los periodos anteriores, "
        "ajustada por día de la semana; |z| > 3.5 es anomalía. Una advertencia se envía si dura "
        "dos periodos; una crítica (|z| ≥ 6), de inmediato. Los datos incompletos no se juzgan."
    )
    regions = alert_regions(view.user)
    if not regions:
        st.info(
            "Las alertas son por región y cada región incluye ventas de otros vendedores: "
            "los vendedores no las reciben.",
            icon="🔒",
        )
        return
    sent = anomaly_alerts(view)
    c = st.columns(3)
    c[0].metric("Alertas en la selección", f"{len(sent):,}")
    c[1].metric("Críticas", f"{(sent['severity'] == 'critical').sum():,}")
    regional = sent[sent["region"] != "Brasil"]["region"] if len(sent) else pd.Series()
    c[2].metric("Regiones con alertas", f"{regional.nunique()}")

    left, right = st.columns([2, 1])
    with right:
        series_id = st.selectbox(
            "Serie",
            list(SERIES_LABELS),
            index=list(SERIES_LABELS).index("on_time_delivery"),  # where the crises show
            format_func=SERIES_LABELS.get,
            key="anomaly_series",
        )
        region = st.selectbox("Región", regions, key="anomaly_region")
    with left:
        df = anomaly_series(view, series_id, region)
        unit = {
            "orders": "count",
            "gmv": "currency",
            "on_time_delivery": "ratio",
            "negative_reviews": "ratio",
            "carrier_pickups": "count",
            "late_dispatch": "ratio",
        }[series_id]
        fig = go.Figure()
        band = df.dropna(subset=["low", "high"])
        fig.add_scatter(
            x=band["date"], y=band["high"], line=dict(width=0), showlegend=False, hoverinfo="skip"
        )
        fig.add_scatter(
            x=band["date"],
            y=band["low"],
            fill="tonexty",
            line=dict(width=0),
            fillcolor="rgba(37,99,235,0.12)",
            name="Rango normal",
            hoverinfo="skip",
        )
        fig.add_scatter(
            x=df["date"],
            y=df["value"],
            name="Valor",
            mode="lines",
            line=dict(color=BLUE, width=1.5),
        )
        for status, color, label in (
            (
                "anomaly_up",
                RED if unit == "ratio" and series_id == "negative_reviews" else GREEN,
                "Anomalía ↑",
            ),
            ("anomaly_down", RED, "Anomalía ↓"),
        ):
            pts = df[df["status"] == status]
            if len(pts):
                fig.add_scatter(
                    x=pts["date"],
                    y=pts["value"],
                    mode="markers",
                    name=label,
                    marker=dict(color=color, size=9, line=dict(color="white", width=1)),
                )
        # One grey band per run of incomplete points (the start and the end of the data).
        incomplete = (df["status"] == "incomplete").to_numpy()
        runs = (incomplete != np.roll(incomplete, 1)).cumsum()
        for _, run in df[incomplete].groupby(runs[incomplete]):
            fig.add_vrect(
                x0=run["date"].min(),
                x1=run["date"].max(),
                fillcolor=GRAY,
                opacity=0.15,
                line_width=0,
                annotation_text="datos incompletos",
            )
        # Registered events: planned in blue, unplanned in orange (config/events.yaml).
        shown = df["date"]
        for ev in event_calendar().itertuples():
            lo, hi = pd.Timestamp(ev.start), pd.Timestamp(ev.end)
            if len(shown) and hi >= shown.min() and lo <= shown.max():
                fig.add_vrect(
                    x0=lo,
                    x1=hi,
                    line_width=0,
                    opacity=0.18,
                    fillcolor="#93c5fd" if ev.kind == "planned" else "#fdba74",
                )  # unlabeled: back-to-back events' labels overlap; the caption names them
        planned_pts = df[df["status"] == "planned"]
        if len(planned_pts):
            fig.add_scatter(
                x=planned_pts["date"],
                y=planned_pts["value"],
                mode="markers",
                name="Esperado por un evento planeado",
                marker=dict(color="#60a5fa", size=8, symbol="diamond"),
            )
        if unit == "ratio":
            fig.update_yaxes(tickformat=".0%")
        fig.update_layout(title=f"{SERIES_LABELS[series_id]} · {region}")
        chart(fig, 380)
        cal = event_calendar()
        planned = ", ".join(cal.loc[cal["kind"] == "planned", "name"])
        unplanned = ", ".join(cal.loc[cal["kind"] == "unplanned", "name"])
        st.caption(
            f"Bandas azules — eventos planeados: {planned or 'ninguno'}. Naranjas — no planeados: "
            f"{unplanned or 'ninguno'}. Gris: datos incompletos. Detalle en la pestaña Eventos."
        )

    if len(sent):
        st.markdown("##### Alertas enviadas (más recientes primero)")
        st.dataframe(
            pd.DataFrame(
                {
                    "Enviada": pd.to_datetime(sent["sent_on"]).dt.strftime("%d/%m/%Y"),
                    "": sent["severity"].map({"critical": "🔴", "warning": "🟠"}),
                    "Alerta": sent["subject"].str.replace("[BI] ", "", regex=False).str[2:],
                    "Responsable": sent["owner"].fillna(""),
                    "En riesgo": sent["at_risk"].map(lambda n: "" if pd.isna(n) else f"{n:,.0f}"),
                    "Evento": sent["event"].fillna(""),
                    "Para": sent["recipients"].map(lambda r: ", ".join(r)),
                }
            ),
            hide_index=True,
            width="stretch",
            height=320,
        )
    else:
        st.success("Sin alertas de anomalías en la selección.")


def events_section(view: View) -> None:
    st.caption(
        "Calendario de eventos (config/events.yaml). Un evento planeado no alerta por lo que se "
        "espera de él (el pico del Black Friday) pero sí por lo demás; uno no planeado se "
        "registra al confirmar una alerta y queda fuera de la línea base mientras dura."
    )
    cal = event_calendar()
    if cal.empty:
        st.info("No hay eventos registrados.")
        return
    kinds = {"planned": "Planeado", "unplanned": "No planeado"}
    if view.user.role not in data.QUALITY_ROLES:
        st.dataframe(
            pd.DataFrame(
                {
                    "Evento": cal["name"],
                    "Tipo": cal["kind"].map(kinds),
                    "Del": pd.to_datetime(cal["start"]).dt.strftime("%d/%m/%Y"),
                    "Al": pd.to_datetime(cal["end"]).dt.strftime("%d/%m/%Y"),
                }
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption("El análisis de impacto es nacional: lo ven dirección y analistas.")
        return
    imp = event_impact(view.user)

    def pct(x):
        return "" if pd.isna(x) else f"{x:+.0%}"

    st.markdown("##### Qué costó cada evento (Brasil, contra lo que el detector esperaba)")
    st.dataframe(
        pd.DataFrame(
            {
                "Evento": imp["event"],
                "Tipo": imp["kind"].map(kinds),
                "Del": pd.to_datetime(imp["start"]).dt.strftime("%d/%m/%Y"),
                "Al": pd.to_datetime(imp["end"]).dt.strftime("%d/%m/%Y"),
                "Pedidos": imp["orders_delta_pct"].map(pct),
                "Despachos": imp["pickups_delta_pct"].map(pct),
                "Entregas tarde extra": imp["late_orders_extra"].map(
                    lambda x: "" if pd.isna(x) else f"{x:,.0f}"
                ),
                "Calificación": [
                    "" if pd.isna(a) or pd.isna(b) else f"{a:.2f} → {b:.2f}"
                    for a, b in zip(imp["score_before"], imp["score_during"], strict=True)
                ],
            }
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "Pedidos y despachos: durante el evento. Entregas tarde: pedidos que vencían del inicio "
        "hasta 14 días después del fin, contra la puntualidad esperada. Calificación: 8 semanas "
        "antes contra el evento y sus 14 días posteriores."
    )
    notes = cal[cal["note"].fillna("") != ""]
    for ev in notes.itertuples():
        st.markdown(f"**{ev.name}.** {ev.note}")


def target_section(view: View) -> None:
    st.caption(
        "Reglas contra la meta de cada KPI (config/kpis.yaml), por mes completo y por región, "
        f"con al menos {data.MIN_ORDERS} pedidos."
    )
    a = alerts(view)
    status = target_status(view, "month")
    if len(status):
        names = {k.id: k.name for k in CATALOG.values()}
        months = sorted(status["key"].unique())
        kpi_ids = list(dict.fromkeys(status["kpi"]))
        code = {"ok": 1, "off_target": -1}
        z, labels = [], []
        for kpi_id in kpi_ids:
            rows = status[status["kpi"] == kpi_id].set_index("key")
            z.append([code.get(rows.at[m, "status"], 0) for m in months])
            labels.append(
                [text.fmt(rows.at[m, "value"], rows.at[m, "unit"], view.currency) for m in months]
            )
        fig = go.Figure(
            go.Heatmap(
                z=z,
                x=[text.month_label(m) for m in months],
                y=[names[k] for k in kpi_ids],
                text=labels,
                texttemplate="%{text}",
                colorscale=[[0, "#fecaca"], [0.5, "#f1f5f9"], [1, "#bbf7d0"]],
                zmin=-1,
                zmax=1,
                showscale=False,
                xgap=2,
                ygap=2,
            )
        )
        fig.update_layout(
            title=f"KPIs con meta, mes a mes (rojo: fuera de meta; gris: menos de "
            f"{data.MIN_ORDERS} pedidos)"
        )
        fig.update_xaxes(tickangle=-45)
        chart(fig, 380)

    st.markdown(f"##### {len(a)} alertas en la selección")
    if len(a):
        shown = a.sort_values(["scope", "key", "kpi"]).assign(
            Cuándo_o_dónde=lambda d: [
                text.month_label(k) if s == "Mes" else k
                for s, k in zip(d["scope"], d["key"], strict=True)
            ],
            Valor=lambda d: [
                text.fmt(x, u, view.currency) for x, u in zip(d["value"], d["unit"], strict=True)
            ],
            Meta=lambda d: [
                text.fmt(x, u, view.currency) for x, u in zip(d["target"], d["unit"], strict=True)
            ],
        )
        st.dataframe(
            shown[["scope", "Cuándo_o_dónde", "name", "Valor", "Meta"]].rename(
                columns={"scope": "Corte", "Cuándo_o_dónde": "Cuándo / dónde", "name": "KPI"}
            ),
            hide_index=True,
            width="stretch",
        )


# --- page 5: data quality -------------------------------------------------------------------


def page_quality(view: View) -> None:
    st.title("Calidad de datos")
    if view.user.role not in data.QUALITY_ROLES:
        st.warning(
            "El reporte de calidad cuenta pedidos de todas las regiones y vendedores: "
            "solo lo ven dirección y analistas.",
            icon="🔒",
        )
        return
    with db() as con:
        report = data.quality_report(con, view.user)
        recon = data.reconciliation(con, view.user)
        quarantined = data.quarantine_rows(con, view.user)
        comp = data.monthly_completeness(con, view.user)
    c = st.columns(4)
    c[0].metric("Pruebas", f"{len(report)}")
    c[1].metric("Con hallazgos", f"{(report['rows'] > 0).sum()}")
    c[2].metric("Pedidos en cuarentena", f"{len(quarantined):,}")
    ok = bool(recon["ok"].all())
    c[3].metric("Conciliación", "✅ cuadra" if ok else "❌ no cuadra")
    st.caption(
        "Pedidos que se contradicen a sí mismos van a cuarentena con su motivo y no llegan a "
        "los KPIs; la conciliación comprueba que staging = marts + cuarentena, al centavo."
    )

    shown = report.assign(
        Severidad=report["severity"].map(SEVERITY_LABELS),
        Filas=report["rows"].map("{:,}".format),
        Revisadas=report["total"].map("{:,}".format),
        Porcentaje=report["pct"].map(lambda x: f"{x:.3f} %"),
        Valor=report["value_brl"].map(lambda x: text.money(x, "BRL", compact=False) if x else ""),
    )
    st.dataframe(
        shown[
            ["Severidad", "check_id", "description", "Filas", "Revisadas", "Porcentaje", "Valor"]
        ].rename(columns={"check_id": "Prueba", "description": "Qué significa"}),
        hide_index=True,
        width="stretch",
        height=420,
    )

    comp["mes"] = pd.to_datetime(comp["year_month"].astype(str), format="%Y%m")
    comp["Estado"] = comp["complete"].map({True: "Completo", False: "Incompleto"})
    fig = px.bar(
        comp,
        x="mes",
        y="orders",
        color="Estado",
        color_discrete_map={"Completo": BLUE, "Incompleto": ORANGE},
        title="Pedidos por mes: los meses incompletos no se comparan",
        labels={"orders": "Pedidos", "mes": ""},
    )
    chart(fig, 300)
    thin = comp[~comp["complete"]]
    st.caption(
        "Incompletos (barras casi invisibles): "
        + ", ".join(
            f"{text.month_label(m)} ({n:,})"
            for m, n in zip(thin["year_month"], thin["orders"], strict=True)
        )
        + ". Las tendencias, metas y alertas los excluyen."
    )

    left, right = st.columns(2)
    with left:
        st.markdown("##### Conciliación")
        st.dataframe(
            pd.DataFrame(
                {
                    "": recon["ok"].map({True: "✅", False: "❌"}),
                    "Comprobación": recon["name"],
                    "Esperado": recon["expected"].map("{:,.2f}".format),
                    "Real": recon["actual"].map("{:,.2f}".format),
                }
            ),
            hide_index=True,
            width="stretch",
        )
    with right:
        st.markdown(f"##### {len(quarantined)} pedidos en cuarentena")
        st.dataframe(
            pd.DataFrame(
                {
                    "Pedido": quarantined["order_id"].str[:10] + "…",
                    "Estatus": quarantined["order_status"],
                    "Compra": quarantined["purchased_at"].dt.strftime("%d/%m/%Y"),
                    "Motivo": quarantined["reasons"],
                }
            ),
            hide_index=True,
            width="stretch",
            height=250,
        )


# --- navigation -----------------------------------------------------------------------------

PAGES = {
    "resumen": ("Resumen ejecutivo", "🏠", page_summary),
    "ventas": ("Ventas", "💰", page_sales),
    "operacion": ("Operación y satisfacción", "🚚", page_operations),
    "alertas": ("Alertas", "🚨", page_alerts),
    "calidad": ("Calidad de datos", "🧪", page_quality),
}


def main() -> None:
    st.set_page_config(page_title="BI & KPIs · Olist", page_icon="📊", layout="wide")
    refresh_if_reloaded()
    view = sidebar()
    # The view is bound per run (default argument), never kept in a module global: two
    # people using the dashboard at once must not see each other's selection.
    pages = [
        st.Page(
            lambda fn=fn, v=view: fn(v),
            title=title,
            icon=icon,
            url_path=path,
            default=path == "resumen",
        )
        for path, (title, icon, fn) in PAGES.items()
    ]
    st.navigation(pages, position="top").run()


def render(path: str) -> None:
    """One page without navigation (used by the tests)."""
    st.set_page_config(page_title="BI & KPIs · Olist", layout="wide")
    refresh_if_reloaded()
    PAGES[path][2](sidebar())
