"""Who sees what. Each area gets its own board: sales never sees costs, the shop never sees
prices, a seller sees only their own clients.

The restriction is applied once, to the data, before any page runs: a page can't show what
the dataset it receives no longer has.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pandas as pd

from mto_bi.dataset import Dataset

SUMMARY, QUOTES, ORDERS, SHOP, RISK, MARGIN, BILLING, UPLOAD = (
    "resumen",
    "cotizaciones",
    "pedidos",
    "taller",
    "riesgo",
    "margen",
    "facturacion",
    "cargar",
)
PRICE_COLUMNS = {"precio_venta", "importe", "folio_factura"}


@dataclass(frozen=True)
class Role:
    key: str
    label: str
    description: str
    pages: tuple[str, ...]
    uploads: tuple[str, ...]  # templates this role may load
    sees_prices: bool = True
    sees_costs: bool = False
    sees_invoices: bool = False
    seller: str | None = None


ROLES = (
    Role(
        "direccion",
        "Dirección",
        "Todo: ventas, taller, margen y facturación.",
        (SUMMARY, QUOTES, ORDERS, SHOP, RISK, MARGIN, BILLING, UPLOAD),
        ("cotizaciones", "pedidos", "produccion", "costos", "facturas"),
        sees_costs=True,
        sees_invoices=True,
    ),
    Role(
        "ventas",
        "Ventas",
        "Cotizaciones, pedidos y entregas de todo el equipo. Sin costos ni margen.",
        (SUMMARY, QUOTES, ORDERS, RISK, UPLOAD),
        ("cotizaciones", "pedidos"),
    ),
    Role(
        "vendedor",
        "Vendedora: Ana Torres",
        "Solo sus clientes, sus cotizaciones y sus pedidos.",
        (SUMMARY, QUOTES, ORDERS, RISK),
        (),
        seller="Ana Torres",
    ),
    Role(
        "taller",
        "Taller",
        "Producción y pedidos por entregar. Sin precios.",
        (SHOP, RISK, UPLOAD),
        ("produccion",),
        sees_prices=False,
    ),
    Role(
        "administracion",
        "Administración",
        "Facturación, costos y margen.",
        (SUMMARY, ORDERS, MARGIN, BILLING, UPLOAD),
        ("costos", "facturas", "pedidos"),
        sees_costs=True,
        sees_invoices=True,
    ),
)
BY_KEY = {r.key: r for r in ROLES}


def scope(data: Dataset, role: Role) -> Dataset:
    """The dataset as this role is allowed to see it."""
    quotes, orders = data.quotes, data.orders
    production, costs, invoices = data.production, data.costs, data.invoices
    if role.seller:
        quotes = quotes[quotes["vendedor"] == role.seller]
        orders = orders[orders["vendedor"] == role.seller]
        mine = set(orders["folio_pedido"])
        production = production[production["folio_pedido"].isin(mine)]
        costs = costs[costs["folio_pedido"].isin(mine)]
    if QUOTES not in role.pages:
        quotes = quotes.iloc[0:0]
    if not role.sees_prices:
        quotes = quotes.drop(columns=[c for c in PRICE_COLUMNS if c in quotes], errors="ignore")
        orders = orders.drop(columns=[c for c in PRICE_COLUMNS if c in orders], errors="ignore")
    if not role.sees_costs:
        costs = costs.iloc[0:0]
    if not role.sees_invoices:
        invoices = invoices.iloc[0:0]
    return replace(
        data,
        quotes=quotes,
        orders=orders,
        production=production,
        costs=costs,
        invoices=invoices,
    )


def can_see(df: pd.DataFrame, column: str) -> bool:
    return column in df.columns
