"""The four Excel templates: one definition drives the template files, the instructions sheet
and the validation of what people upload.

Each template is one row per line, the way people already keep these lists in Excel: a quote
or an order with three pieces is three rows sharing a folio.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

TEXT, DATE, INT, MONEY, CHOICE = "text", "date", "int", "money", "choice"

CLIENT_TYPES = ("Diseñador", "Arquitecto", "Particular", "Empresa")
QUOTE_STATES = ("Abierta", "Ganada", "Perdida")
# The order a piece moves through the shop. A piece may skip a stage (a table has no
# upholstery), but never goes back to an earlier one.
STAGES = ("Compra de materiales", "Carpintería", "Tapicería", "Acabado", "Control de calidad")
COST_CONCEPTS = ("Materiales", "Tela", "Mano de obra", "Flete", "Instalación", "Otro")


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str
    required: bool
    help: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Template:
    name: str
    label: str
    purpose: str
    columns: tuple[Column, ...]
    key: tuple[str, ...]  # columns that identify a row; duplicates are refused

    def column(self, key: str) -> Column:
        return next(c for c in self.columns if c.key == key)


QUOTES = Template(
    "cotizaciones",
    "Cotizaciones",
    "Cada pieza cotizada, ganada o no. Sirve para medir la tasa de cierre.",
    (
        Column("folio", "Folio", TEXT, True, "Folio de la cotización. Se repite en cada pieza."),
        Column(
            "partida", "Partida", INT, True, "Número de la pieza dentro de la cotización: 1, 2…"
        ),
        Column("fecha", "Fecha", DATE, True, "Fecha en que se envió la cotización."),
        Column("cliente", "Cliente", TEXT, True, "Nombre del cliente o del despacho."),
        Column(
            "tipo_cliente",
            "Tipo de cliente",
            CHOICE,
            False,
            "Quién compra.",
            CLIENT_TYPES,
        ),
        Column("vendedor", "Vendedor", TEXT, True, "Quien atiende al cliente."),
        Column("marca", "Marca", TEXT, True, "Marca o línea de la pieza."),
        Column("pieza", "Pieza", TEXT, True, "Modelo, por ejemplo: Sofá Lino 3 plazas."),
        Column("cantidad", "Cantidad", INT, True, "Unidades de esa pieza."),
        Column("importe", "Importe", MONEY, True, "Total de la partida, sin IVA, en pesos."),
        Column(
            "estado",
            "Estado",
            CHOICE,
            True,
            "Abierta mientras el cliente no decide.",
            QUOTE_STATES,
        ),
        Column(
            "fecha_cierre",
            "Fecha de cierre",
            DATE,
            False,
            "Cuándo se ganó o se perdió. Obligatoria si no está Abierta.",
        ),
        Column("motivo_perdida", "Motivo de pérdida", TEXT, False, "Si se perdió, por qué."),
    ),
    ("folio", "partida"),
)

ORDERS = Template(
    "pedidos",
    "Pedidos",
    "Cada pieza vendida, con la fecha prometida y la de entrega. Mide ventas y puntualidad.",
    (
        Column("folio_pedido", "Folio de pedido", TEXT, True, "Se repite en cada pieza."),
        Column("partida", "Partida", INT, True, "Número de la pieza dentro del pedido: 1, 2…"),
        Column(
            "folio_cotizacion",
            "Folio de cotización",
            TEXT,
            False,
            "La cotización de la que salió, si la hubo.",
        ),
        Column("fecha_pedido", "Fecha de pedido", DATE, True, "Cuándo se confirmó el pedido."),
        Column("cliente", "Cliente", TEXT, True, "Nombre del cliente o del despacho."),
        Column("vendedor", "Vendedor", TEXT, True, "Quien vendió."),
        Column("marca", "Marca", TEXT, True, "Marca o línea de la pieza."),
        Column("pieza", "Pieza", TEXT, True, "Modelo, por ejemplo: Sofá Lino 3 plazas."),
        Column("cantidad", "Cantidad", INT, True, "Unidades de esa pieza."),
        Column("precio_venta", "Precio de venta", MONEY, True, "Total de la partida, sin IVA."),
        Column(
            "fecha_prometida", "Fecha prometida", DATE, True, "La fecha que se le dio al cliente."
        ),
        Column(
            "fecha_entrega",
            "Fecha de entrega",
            DATE,
            False,
            "Vacía mientras no se entrega.",
        ),
        Column(
            "folio_factura",
            "Folio de factura",
            TEXT,
            False,
            "Serie y folio (A-1234) o el UUID de la factura.",
        ),
    ),
    ("folio_pedido", "partida"),
)

PRODUCTION = Template(
    "produccion",
    "Producción",
    "Cada etapa del taller por la que pasa cada pieza. Mide tiempos y cuellos de botella.",
    (
        Column("folio_pedido", "Folio de pedido", TEXT, True, "El pedido de la pieza."),
        Column("partida", "Partida", INT, True, "La partida del pedido."),
        Column("etapa", "Etapa", CHOICE, True, "Etapa del taller.", STAGES),
        Column("fecha_inicio", "Fecha de inicio", DATE, True, "Cuándo empezó la etapa."),
        Column("fecha_fin", "Fecha de fin", DATE, False, "Vacía mientras sigue en esa etapa."),
        Column("responsable", "Responsable", TEXT, False, "Quién la hizo."),
    ),
    ("folio_pedido", "partida", "etapa"),
)

COSTS = Template(
    "costos",
    "Costos",
    "Lo que costó hacer cada pedido. Con esto se calcula el margen.",
    (
        Column(
            "folio_pedido", "Folio de pedido", TEXT, True, "El pedido al que se carga el costo."
        ),
        Column(
            "partida",
            "Partida",
            INT,
            False,
            "Vacía si el costo es de todo el pedido: se reparte según el precio de cada pieza.",
        ),
        Column("concepto", "Concepto", CHOICE, True, "Tipo de costo.", COST_CONCEPTS),
        Column("importe", "Importe", MONEY, True, "Sin IVA, en pesos."),
        Column("fecha", "Fecha", DATE, False, "Fecha del gasto."),
        Column("proveedor", "Proveedor", TEXT, False, "A quién se le pagó."),
    ),
    (),
)

TEMPLATES = (QUOTES, ORDERS, PRODUCTION, COSTS)
BY_NAME = {t.name: t for t in TEMPLATES}


def normalize(text: str) -> str:
    """'Fecha de entrega ' -> 'fecha_de_entrega': how headers are matched, so accents, case
    and spacing typed by hand don't matter."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def folio_key(value) -> str:
    """'a-1234 ' and 'A1234' are the same folio; so are a UUID in either case."""
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())
