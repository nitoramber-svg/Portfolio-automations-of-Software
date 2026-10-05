"""A made-up furniture workshop: two years of quotes, orders, shop-floor stages, costs and
invoices, written as the same Excel templates and CFDI XML a real shop would upload.

Brands, pieces, clients and sellers are invented. The data carries stories the dashboard has to
find, so the demo shows something real:

* Upholstery is the bottleneck every October–December, and deliveries slip.
* Imported fabric got dearer and slower over the last six months: Atelier Lino's margin falls
  and its purchasing stage lengthens.
* Now and then a stage gets stuck (a fabric out of stock, a piece redone) and an order runs
  past its promise.
* One seller closes more quotes by discounting, and earns less margin.
* Designers close far more often than private clients; very large quotes close less.
* A handful of delivered orders were never invoiced.
* The files carry a few typing mistakes, so the upload report has something to show.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date
from xml.sax.saxutils import quoteattr

import numpy as np
import pandas as pd

from mto_bi.schema import COSTS, ORDERS, PRODUCTION, QUOTES
from mto_bi.templates import workbook_bytes

BUY, CARPENTRY, UPHOLSTERY, FINISH, QC = (
    "Compra de materiales",
    "Carpintería",
    "Tapicería",
    "Acabado",
    "Control de calidad",
)
STAGE_DAYS = {BUY: 5, CARPENTRY: 7, UPHOLSTERY: 8, FINISH: 6, QC: 2}
SEASON = (0.7, 0.8, 0.9, 0.9, 1.0, 0.9, 0.9, 1.0, 1.2, 1.3, 1.3, 0.9)
EMITTER_RFC = "EKU9003173C9"  # the SAT's published test RFC
PUBLIC_RFC = "XAXX010101000"  # "público en general"


@dataclass(frozen=True)
class Brand:
    name: str
    route: tuple[str, ...]
    promise_days: int
    materials: float  # share of list price
    fabric: float
    labor: float


BRANDS = {
    "Nogal & Co.": Brand("Nogal & Co.", (BUY, CARPENTRY, FINISH, QC), 35, 0.27, 0.0, 0.18),
    "Atelier Lino": Brand("Atelier Lino", (BUY, CARPENTRY, UPHOLSTERY, QC), 35, 0.12, 0.16, 0.18),
    "Bruma": Brand("Bruma", (BUY, CARPENTRY, UPHOLSTERY, FINISH, QC), 49, 0.15, 0.08, 0.17),
}
# (brand, piece, list price in MXN, usual quantity range)
CATALOG = (
    ("Nogal & Co.", "Mesa de comedor Roble 8 personas", 68_000, (1, 1)),
    ("Nogal & Co.", "Credenza Nogal 4 puertas", 46_000, (1, 1)),
    ("Nogal & Co.", "Cama King Nogal", 58_000, (1, 1)),
    ("Nogal & Co.", "Buró Nogal", 14_500, (1, 2)),
    ("Nogal & Co.", "Librero Roble", 39_000, (1, 2)),
    ("Atelier Lino", "Sofá Lino 3 plazas", 72_000, (1, 1)),
    ("Atelier Lino", "Sillón Lino", 31_000, (1, 2)),
    ("Atelier Lino", "Cabecera tapizada King", 26_000, (1, 1)),
    ("Atelier Lino", "Banca tapizada", 18_500, (1, 2)),
    ("Atelier Lino", "Sofá modular 5 piezas", 145_000, (1, 1)),
    ("Bruma", "Sofá Bruma curvo", 168_000, (1, 1)),
    ("Bruma", "Silla Bruma tapizada", 16_800, (4, 10)),
    ("Bruma", "Consola Bruma mármol", 92_000, (1, 1)),
    ("Bruma", "Mesa de centro Bruma", 54_000, (1, 1)),
    ("Bruma", "Sillón Bruma lounge", 64_000, (1, 2)),
)
SELLERS = {  # name: (share of quotes, discount on list price, close-rate multiplier)
    "Ana Torres": (0.30, 0.00, 1.00),
    "Luis Mendoza": (0.25, 0.02, 0.95),
    "Sofía Ramírez": (0.25, 0.00, 1.05),
    "Diego Herrera": (0.20, 0.11, 1.35),
}
CLIENT_TYPES = {  # type: (share of quotes, base close rate)
    "Diseñador": (0.45, 0.44),
    "Arquitecto": (0.20, 0.33),
    "Particular": (0.25, 0.18),
    "Empresa": (0.10, 0.30),
}
LOSS_REASONS = ("Precio", "Tiempo de entrega", "Eligió otro proveedor", "Proyecto cancelado")
_WORDS = [
    "Alba",
    "Ceiba",
    "Olmo",
    "Arce",
    "Lumen",
    "Nativa",
    "Duna",
    "Cantera",
    "Piedra",
    "Azul",
    "Bambú",
    "Marea",
    "Salvia",
    "Encino",
    "Jacaranda",
    "Nube",
    "Terra",
    "Brisa",
    "Ocre",
    "Plata",
    "Cobre",
    "Tule",
    "Lirio",
    "Basalto",
    "Coral",
    "Selva",
    "Musgo",
    "Ámbar",
    "Hueso",
    "Sal",
    "Tierra",
    "Roble",
    "Laurel",
    "Cedro",
    "Agave",
    "Ónix",
    "Aurora",
    "Bruma",
    "Niebla",
    "Pino",
]
_FIRST = [
    "Mariana",
    "Rodrigo",
    "Valeria",
    "Andrés",
    "Fernanda",
    "Jorge",
    "Paola",
    "Ricardo",
    "Daniela",
    "Emilio",
    "Regina",
    "Tomás",
]
_LAST = [
    "García",
    "Navarro",
    "Castillo",
    "Ortega",
    "Vargas",
    "Ruiz",
    "Flores",
    "Herrera",
    "Morales",
    "Salinas",
    "Rojas",
    "Ibarra",
]


def clients(rng) -> dict[str, list[str]]:
    words = list(rng.permutation(_WORDS))
    people = {f"{f} {last}" for f in _FIRST for last in _LAST}
    return {
        "Diseñador": [f"Estudio {w}" for w in words[:26]],
        "Arquitecto": [f"{w} Arquitectos" for w in words[26:36]],
        "Particular": sorted(rng.choice(sorted(people), 60, replace=False)),
        "Empresa": [f"Hotel {w}" for w in words[36:40]]
        + [f"Restaurante {w}" for w in words[:4]]
        + [f"Oficinas {w}" for w in words[4:8]],
    }


@dataclass
class Sample:
    quotes: pd.DataFrame
    orders: pd.DataFrame
    production: pd.DataFrame
    costs: pd.DataFrame
    invoices: list[tuple[str, bytes]]
    as_of: date


def generate(as_of: date | None = None, months: int = 24, seed: int = 7) -> Sample:
    as_of = as_of or date.today()
    rng = np.random.default_rng(seed)
    start = pd.Timestamp(as_of) - pd.DateOffset(months=months)
    end = pd.Timestamp(as_of)
    fabric_shock = end - pd.Timedelta(days=180)
    names = clients(rng)
    catalog = pd.DataFrame(CATALOG, columns=["marca", "pieza", "lista", "qty"])
    sellers = list(SELLERS)
    seller_p = np.array([SELLERS[s][0] for s in sellers])
    types = list(CLIENT_TYPES)
    type_p = np.array([CLIENT_TYPES[t][0] for t in types])

    quotes, orders = [], []
    q_no, p_no = 100, 40
    for day in pd.date_range(start, end - pd.Timedelta(days=1), freq="D"):
        if day.weekday() == 6:
            continue
        growth = 1 + 0.15 * (day - start).days / 365
        for _ in range(rng.poisson(1.6 * SEASON[day.month - 1] * growth)):
            q_no += 1
            folio = f"C-{q_no:05d}"
            ctype = types[rng.choice(len(types), p=type_p)]
            client = rng.choice(names[ctype])
            seller = sellers[rng.choice(len(sellers), p=seller_p)]
            _, discount, close_mult = SELLERS[seller]
            brand = rng.choice(list(BRANDS))
            options = catalog[catalog["marca"] == brand]
            n_lines = int(rng.choice([1, 2, 3, 4], p=[0.4, 0.3, 0.2, 0.1]))
            picks = options.sample(n=min(n_lines, len(options)), random_state=rng)
            lines = []
            for i, row in enumerate(picks.itertuples(), start=1):
                qty = int(rng.integers(row.qty[0], row.qty[1] + 1))
                listed = row.lista * qty * rng.uniform(0.9, 1.15)
                lines.append(
                    {
                        "folio": folio,
                        "partida": i,
                        "fecha": day,
                        "cliente": client,
                        "tipo_cliente": ctype,
                        "vendedor": seller,
                        "marca": brand,
                        "pieza": row.pieza,
                        "cantidad": qty,
                        "lista": round(listed, 2),
                        "importe": round(listed * (1 - discount), -1),
                    }
                )
            total = sum(x["importe"] for x in lines)
            p_win = CLIENT_TYPES[ctype][1] * close_mult * (0.65 if total > 250_000 else 1.0)
            won = rng.random() < min(p_win, 0.85)
            lag = int(rng.integers(4, 35) if won else rng.integers(8, 60))
            closes = day + pd.Timedelta(days=lag)
            forgotten = not won and rng.random() < 0.06  # nobody followed it up
            if closes > end or forgotten:
                for x in lines:
                    x.update(estado="Abierta", fecha_cierre=pd.NaT, motivo_perdida=None)
                quotes += lines
                continue
            reason = rng.choice(LOSS_REASONS, p=[0.45, 0.25, 0.2, 0.1])
            won_lines = [x for x in lines if rng.random() < 0.85] or lines[:1] if won else []
            for x in lines:
                is_won = any(x is w for w in won_lines)
                x.update(
                    estado="Ganada" if is_won else "Perdida",
                    fecha_cierre=closes,
                    motivo_perdida=None if is_won else str(reason if not won else "Precio"),
                )
            quotes += lines
            if won_lines:
                p_no += 1
                orders += _order(f"P-{p_no:05d}", folio, closes, won_lines, rng)

    # A few repeat orders that never went through a quote.
    repeat = pd.DataFrame(orders).drop_duplicates("folio_pedido").sample(frac=0.08, random_state=1)
    for r in repeat.itertuples():
        day = r.fecha_pedido + pd.Timedelta(days=int(rng.integers(60, 200)))
        if day >= end:
            continue
        p_no += 1
        piece = catalog[catalog["marca"] == r.marca].sample(1, random_state=rng).iloc[0]
        line = {
            "cliente": r.cliente,
            "tipo_cliente": r.tipo_cliente,
            "vendedor": r.vendedor,
            "marca": r.marca,
            "pieza": piece.pieza,
            "cantidad": 1,
            "lista": float(piece.lista),
            "importe": float(piece.lista) * (1 - SELLERS[r.vendedor][1]),
        }
        orders += _order(f"P-{p_no:05d}", None, day, [line], rng)

    quotes_df = pd.DataFrame(quotes)
    orders_df = pd.DataFrame(orders).sort_values(["fecha_pedido", "folio_pedido", "partida"])
    production_df, deliveries = _shop_floor(orders_df, end, fabric_shock, rng)
    orders_df["fecha_entrega"] = [
        deliveries.get((f, p))
        for f, p in zip(orders_df["folio_pedido"], orders_df["partida"], strict=True)
    ]
    costs_df = _costs(orders_df, production_df, fabric_shock, rng)
    orders_df, invoices = _invoices(orders_df, end, rng)
    return Sample(
        quotes=quotes_df,
        orders=orders_df.reset_index(drop=True),
        production=production_df,
        costs=costs_df,
        invoices=invoices,
        as_of=as_of,
    )


def _order(folio, quote, day, lines, rng):
    promise = max(BRANDS[x["marca"]].promise_days for x in lines)
    promise += 7 if sum(x["cantidad"] for x in lines) > 3 else 0
    return [
        {
            "folio_pedido": folio,
            "partida": i,
            "folio_cotizacion": quote,
            "fecha_pedido": day,
            "cliente": x["cliente"],
            "tipo_cliente": x["tipo_cliente"],
            "vendedor": x["vendedor"],
            "marca": x["marca"],
            "pieza": x["pieza"],
            "cantidad": x["cantidad"],
            "lista": x["lista"],
            "precio_venta": round(x["importe"] * rng.uniform(0.97, 1.0), -1),
            "fecha_prometida": day + pd.Timedelta(days=promise),
        }
        for i, x in enumerate(lines, start=1)
    ]


def _shop_floor(orders, end, fabric_shock, rng):
    rows, deliveries = [], {}
    staff = {
        BUY: ["Compras"],
        CARPENTRY: ["Javier", "Rubén", "Óscar"],
        UPHOLSTERY: ["Martha", "Felipe"],
        FINISH: ["Hugo", "Lidia"],
        QC: ["Carmen"],
    }
    for r in orders.itertuples():
        brand = BRANDS[r.marca]
        t = r.fecha_pedido + pd.Timedelta(days=int(rng.integers(1, 4)))
        for stage in brand.route:
            if t > end:
                break
            days = STAGE_DAYS[stage] * rng.lognormal(0, 0.3)
            if stage in (CARPENTRY, UPHOLSTERY):
                days *= 1 + 0.15 * (r.cantidad - 1) ** 0.7
            if stage == UPHOLSTERY and t.month in (10, 11, 12):
                days *= 1.7  # the year-end rush meets two upholsterers
            if stage == BUY and r.marca != "Nogal & Co." and t >= fabric_shock:
                days *= 1.7  # imported fabric takes longer
            if rng.random() < 0.012:
                days *= 3  # stuck: a fabric out of stock, a piece redone
            finish = t + pd.Timedelta(days=max(1, round(days)))
            rows.append(
                {
                    "folio_pedido": r.folio_pedido,
                    "partida": r.partida,
                    "etapa": stage,
                    "fecha_inicio": t,
                    "fecha_fin": finish if finish <= end else pd.NaT,
                    "responsable": rng.choice(staff[stage]),
                }
            )
            if finish > end:
                break
            t = finish + pd.Timedelta(days=int(rng.integers(0, 3)))
        else:
            delivered = t + pd.Timedelta(days=int(rng.integers(1, 5)))
            if delivered <= end:
                deliveries[(r.folio_pedido, r.partida)] = delivered
    return pd.DataFrame(rows), deliveries


def _costs(orders, production, fabric_shock, rng):
    started = set(zip(production["folio_pedido"], production["partida"], strict=True))
    rows = []
    skip_orders = set(
        orders["folio_pedido"].drop_duplicates().sample(frac=0.04, random_state=3)
    )  # costs never captured
    for r in orders.itertuples():
        if (r.folio_pedido, r.partida) not in started or r.folio_pedido in skip_orders:
            continue
        brand = BRANDS[r.marca]
        fabric = brand.fabric * (1.4 if r.fecha_pedido >= fabric_shock else 1.0)
        for concept, share in (
            ("Materiales", brand.materials),
            ("Tela", fabric),
            ("Mano de obra", brand.labor),
        ):
            if share:
                rows.append(
                    {
                        "folio_pedido": r.folio_pedido,
                        "partida": r.partida,
                        "concepto": concept,
                        "importe": round(r.lista * share * rng.uniform(0.92, 1.08), 2),
                        "fecha": r.fecha_pedido + pd.Timedelta(days=int(rng.integers(1, 10))),
                        "proveedor": None,
                    }
                )
    for folio, g in orders.groupby("folio_pedido"):
        if folio in skip_orders or pd.isna(g["fecha_entrega"]).all():
            continue
        delivered = g["fecha_entrega"].max()
        rows.append(
            {
                "folio_pedido": folio,
                "partida": None,
                "concepto": "Flete",
                "importe": round(float(rng.uniform(1_800, 6_500)), 2),
                "fecha": delivered,
                "proveedor": rng.choice(["Mudanzas Express", "Fletes del Valle"]),
            }
        )
        if g["tipo_cliente"].iloc[0] == "Empresa":
            rows.append(
                {
                    "folio_pedido": folio,
                    "partida": None,
                    "concepto": "Instalación",
                    "importe": round(float(g["lista"].sum() * 0.03), 2),
                    "fecha": delivered,
                    "proveedor": None,
                }
            )
    return pd.DataFrame(rows)


def _invoices(orders, end, rng):
    """One invoice per order once every piece is delivered; some are still pending."""
    orders = orders.copy()
    orders["folio_factura"] = None
    xmls = []
    n = 1000
    done = orders.groupby("folio_pedido").filter(lambda g: g["fecha_entrega"].notna().all())
    for _, g in sorted(done.groupby("folio_pedido"), key=lambda kv: kv[1]["fecha_entrega"].max()):
        day = g["fecha_entrega"].max() + pd.Timedelta(days=int(rng.integers(0, 4)))
        recent = (end - day).days < 30
        if day > end or rng.random() < (0.25 if recent else 0.03):
            continue  # not invoiced (yet)
        n += 1
        orders.loc[g.index, "folio_factura"] = f"A-{n}"
        xmls.append((f"A-{n}.xml", _cfdi("I", "A", n, day, g["cliente"].iloc[0], g, rng)))
    # Invoices with no order behind them (repairs, extra cushions…) and payment receipts.
    for k in range(6):
        n += 1
        day = end - pd.Timedelta(days=int(rng.integers(5, 600)))
        line = pd.DataFrame(
            [
                {
                    "pieza": "Servicio de retapizado",
                    "cantidad": 1,
                    "precio_venta": 8_500.0 + 1_000 * k,
                }
            ]
        )
        xmls.append((f"A-{n}.xml", _cfdi("I", "A", n, day, "Público en general", line, rng)))
    for k in range(12):
        day = end - pd.Timedelta(days=int(rng.integers(5, 600)))
        xmls.append((f"P-{k + 1}.xml", _cfdi("P", "P", k + 1, day, "Estudio Alba", None, rng)))
    return orders, xmls


def _cfdi(kind, serie, folio, day, client, lines, rng) -> bytes:
    stamp = str(
        uuid.UUID(int=int(rng.integers(0, 2**63)) << 64 | int(rng.integers(0, 2**63)))
    ).upper()
    when = (day + pd.Timedelta(hours=int(rng.integers(9, 19)))).strftime("%Y-%m-%dT%H:%M:%S")
    concepts, subtotal = [], 0.0
    if lines is not None:
        for x in lines.itertuples():
            amount = float(x.precio_venta)
            subtotal += amount
            unit = amount / x.cantidad
            concepts.append(
                f'<cfdi:Concepto ClaveProdServ="56101500" Cantidad="{x.cantidad}" ClaveUnidad="H87" '
                f'Descripcion={quoteattr(x.pieza)} ValorUnitario="{unit:.2f}" Importe="{amount:.2f}" '
                'ObjetoImp="02"/>'
            )
    iva = round(subtotal * 0.16, 2) if kind == "I" else 0.0
    total = subtotal + iva
    currency = "XXX" if kind == "P" else "MXN"
    body = (
        "".join(concepts)
        if concepts
        else (
            '<cfdi:Concepto ClaveProdServ="84111506" Cantidad="1" ClaveUnidad="ACT" '
            'Descripcion="Pago" ValorUnitario="0" Importe="0" ObjetoImp="01"/>'
        )
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- Factura de demostración generada por mto_bi: no es válida ante el SAT. -->\n"
        '<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" '
        'xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital" Version="4.0" '
        f'Serie="{serie}" Folio="{folio}" Fecha="{when}" SubTotal="{subtotal:.2f}" '
        f'Total="{total:.2f}" Moneda="{currency}" TipoDeComprobante="{kind}" Exportacion="01" '
        'LugarExpedicion="06600">'
        f'<cfdi:Emisor Rfc="{EMITTER_RFC}" Nombre="TALLER DE MUEBLES DEMO" RegimenFiscal="601"/>'
        f'<cfdi:Receptor Rfc="{PUBLIC_RFC}" Nombre={quoteattr(client)} UsoCFDI="G03" '
        'DomicilioFiscalReceptor="06600" RegimenFiscalReceptor="616"/>'
        f"<cfdi:Conceptos>{body}</cfdi:Conceptos>"
        "<cfdi:Complemento>"
        f'<tfd:TimbreFiscalDigital Version="1.1" UUID="{stamp}" FechaTimbrado="{when}" '
        'SelloCFD="DEMO" NoCertificadoSAT="00000000000000000000" SelloSAT="DEMO"/>'
        "</cfdi:Complemento></cfdi:Comprobante>"
    )
    return xml.encode("utf-8")


# --- as files ----------------------------------------------------------------------------------

TYPOS = {  # rows a person might type wrong; the upload report must catch each one
    "cotizaciones": {"estado": "Ganado"},
    "pedidos": {"precio_venta": "pendiente"},
    "produccion": {"folio_pedido": "P-99999"},
}


def files(sample: Sample, typos: bool = True) -> list[tuple[str, bytes]]:
    """The sample as the files a user uploads: four workbooks and the SAT ZIP."""
    tables = {
        QUOTES: sample.quotes,
        ORDERS: sample.orders,
        PRODUCTION: sample.production,
        COSTS: sample.costs,
    }
    out = []
    for template, df in tables.items():
        df = df.copy()
        if typos and template.name in TYPOS:
            bad = df.iloc[[len(df) // 2]].copy()
            if template.name == "produccion":
                bad = bad.assign(**TYPOS[template.name])
            else:
                key = template.key[0]
                bad[key] = bad[key].astype(str) + "-X"
                for col, value in TYPOS[template.name].items():
                    bad[col] = bad[col].astype(object)
                    bad[col] = value
            df = pd.concat([df, bad], ignore_index=True)
        if typos and template.name == "pedidos":
            late = df.iloc[[len(df) // 3]].copy()
            late["folio_pedido"] = late["folio_pedido"] + "-Y"
            late["fecha_prometida"] = late["fecha_pedido"] - pd.Timedelta(days=3)
            df = pd.concat([df, late], ignore_index=True)
        out.append((f"{template.name}.xlsx", workbook_bytes(template, df)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in sample.invoices:
            zf.writestr(name, content)
    out.append(("facturas_sat.zip", buf.getvalue()))
    return out


def write(folder, as_of: date | None = None) -> list[str]:
    """Write the demo files to ``folder`` (``mto sample``)."""
    from pathlib import Path

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    names = []
    for name, content in files(generate(as_of)):
        (folder / name).write_bytes(content)
        names.append(name)
    return names
