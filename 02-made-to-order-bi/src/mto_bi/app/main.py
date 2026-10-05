"""The web app: sidebar (who is looking, which period), one page per area, and the upload.

Without a data folder configured it runs as a demo over a made-up workshop, generated through
the same Excel templates and invoice XML a real shop uploads. What a visitor uploads lives only
in their session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from mto_bi import dataset, roles, sample
from mto_bi.app import fmt
from mto_bi.dataset import Dataset

DATA_DIR = os.environ.get("MTO_DATA_DIR")
# Pin the demo to a date (screenshots); by default it is generated up to today.
DEMO_DATE = os.environ.get("MTO_DEMO_DATE")


@dataclass(frozen=True)
class Context:
    role: roles.Role
    data: Dataset  # what this role may see, filtered by brand / seller, every date
    start: pd.Timestamp
    end: pd.Timestamp
    as_of: date
    demo: bool

    def period(self, df: pd.DataFrame, column: str) -> pd.DataFrame:
        if df.empty:
            return df
        d = df[column]
        return df[(d >= self.start) & (d <= self.end)]


@st.cache_resource(show_spinner="Preparando los datos de demostración…")
def demo_dataset(today: date) -> Dataset:
    return dataset.build(dataset.classify(sample.files(sample.generate(today))))


@st.cache_resource(show_spinner="Leyendo los archivos…")
def folder_dataset(folder: str, stamp: float) -> Dataset:
    files = [
        (p.name, p.read_bytes())
        for p in sorted(Path(folder).iterdir())
        if p.suffix.lower() in (".xlsx", ".csv", ".xml", ".zip")
    ]
    return dataset.build(dataset.classify(files))


def base_dataset() -> tuple[Dataset, bool]:
    if DATA_DIR and Path(DATA_DIR).is_dir():
        stamp = max((p.stat().st_mtime for p in Path(DATA_DIR).iterdir()), default=0.0)
        return folder_dataset(DATA_DIR, stamp), False
    day = date.fromisoformat(DEMO_DATE) if DEMO_DATE else date.today()
    return demo_dataset(day), True


def current_dataset() -> tuple[Dataset, bool]:
    base, demo = base_dataset()
    return st.session_state.get("uploaded", base), demo


def _filter(data: Dataset, brands: list[str], sellers: list[str]) -> Dataset:
    if not brands and not sellers:
        return data
    o = data.orders
    q = data.quotes
    if brands:
        o = o[o["marca"].isin(brands)]
        q = q[q["marca"].isin(brands)]
    if sellers:
        o = o[o["vendedor"].isin(sellers)]
        q = q[q["vendedor"].isin(sellers)]
    keep = set(o["folio_pedido"])
    lines = set(zip(o["folio_pedido"], o["partida"], strict=True))
    pr = data.production
    pr = pr[[k in lines for k in zip(pr["folio_pedido"], pr["partida"], strict=True)]]
    invoices = data.invoices
    if "folio_factura" in o.columns and len(invoices):
        from mto_bi.schema import folio_key

        wanted = {folio_key(v) for v in o["folio_factura"].dropna()}
        invoices = invoices[
            invoices["folio_key"].isin(wanted) | invoices["uuid"].map(folio_key).isin(wanted)
        ]
    return replace(
        data,
        quotes=q,
        orders=o,
        production=pr,
        costs=data.costs[data.costs["folio_pedido"].isin(keep)],
        invoices=invoices,
    )


def sidebar(data: Dataset, demo: bool) -> Context:
    st.sidebar.markdown("### Taller · Tablero")
    keys = [r.key for r in roles.ROLES]
    wanted = st.query_params.get("ver")
    if "role" not in st.session_state and wanted in roles.BY_KEY:
        st.session_state["role"] = wanted  # a link like ?ver=taller opens that area's view
    key = st.sidebar.selectbox(
        "Ver como",
        keys,
        format_func=lambda k: roles.BY_KEY[k].label,
        key="role",
        help="Cada área ve solo lo suyo. En la versión real, cada persona entra con su usuario.",
    )
    role = roles.BY_KEY[key]
    st.sidebar.caption(role.description)

    as_of = data.as_of or date.today()
    scoped = roles.scope(data, role)
    starts = [
        s.min()
        for s in (
            scoped.quotes.get("fecha"),
            scoped.orders.get("fecha_pedido"),
            scoped.production.get("fecha_inicio"),
        )
        if s is not None and s.notna().any()
    ]
    first = min(starts).date() if starts else as_of - timedelta(days=365)
    default_start = max(first, (pd.Timestamp(as_of) - pd.DateOffset(months=12)).date())
    picked = st.sidebar.date_input(
        "Periodo",
        value=(default_start, as_of),
        min_value=first,
        max_value=as_of,
        format="DD/MM/YYYY",
        key="period",
    )
    start, end = picked if isinstance(picked, tuple) and len(picked) == 2 else (picked, picked)
    if not isinstance(start, date):
        start, end = default_start, as_of

    brands = sorted(scoped.orders["marca"].dropna().unique()) if len(scoped.orders) else []
    chosen_brands = st.sidebar.multiselect("Marca", brands, placeholder="Todas")
    chosen_sellers = []
    if not role.seller and "vendedor" in scoped.orders and len(scoped.orders):
        sellers = sorted(scoped.orders["vendedor"].dropna().unique())
        if roles.QUOTES in role.pages or roles.ORDERS in role.pages:
            chosen_sellers = st.sidebar.multiselect("Vendedor", sellers, placeholder="Todos")

    st.sidebar.divider()
    st.sidebar.caption(f"Datos al **{fmt.day(as_of)}**: el último día con algo registrado.")
    if demo:
        st.sidebar.info(
            "Demostración con datos inventados de un taller ficticio. Lo que cargues en "
            "«Cargar datos» solo lo ves tú y se borra al cerrar la página.",
            icon="ℹ️",
        )
    return Context(
        role=role,
        data=_filter(scoped, chosen_brands, chosen_sellers),
        start=pd.Timestamp(start),
        end=pd.Timestamp(end) + timedelta(days=1) - timedelta(seconds=1),
        as_of=as_of,
        demo=demo,
    )


def main() -> None:
    st.set_page_config(page_title="Taller · Tablero", page_icon="🪑", layout="wide")
    from mto_bi.app import pages

    data, demo = current_dataset()
    ctx = sidebar(data, demo)
    titles = {
        roles.SUMMARY: ("Resumen", ":material/dashboard:"),
        roles.QUOTES: ("Cotizaciones", ":material/request_quote:"),
        roles.ORDERS: ("Pedidos y entregas", ":material/local_shipping:"),
        roles.SHOP: ("Taller", ":material/handyman:"),
        roles.RISK: ("Pedidos en riesgo", ":material/warning:"),
        roles.MARGIN: ("Margen", ":material/payments:"),
        roles.BILLING: ("Facturación", ":material/receipt_long:"),
        roles.UPLOAD: ("Cargar datos", ":material/upload_file:"),
    }
    nav = [
        st.Page(
            (lambda p=p: pages.PAGES[p](ctx)),
            title=titles[p][0],
            icon=titles[p][1],
            url_path=p,
            default=i == 0,
        )
        for i, p in enumerate(ctx.role.pages)
    ]
    st.navigation(nav, position="top").run()
