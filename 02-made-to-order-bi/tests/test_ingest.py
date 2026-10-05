"""Reading what people type into the templates: tolerant with form, strict with meaning, and
always pointing at the Excel row to fix."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from openpyxl import Workbook

from mto_bi import ingest
from mto_bi.ingest import ERROR, parse_file
from mto_bi.schema import ORDERS, QUOTES, TEMPLATES
from mto_bi.templates import workbook_bytes


def xlsx(headers, rows) -> bytes:
    import io

    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ORDER_HEADERS = [c.label for c in ORDERS.columns]


def order_row(**kw):
    base = {
        "folio_pedido": "P-1",
        "partida": 1,
        "folio_cotizacion": None,
        "fecha_pedido": "01/03/2026",
        "cliente": "Estudio Alba",
        "vendedor": "Ana",
        "marca": "Bruma",
        "pieza": "Sofá",
        "cantidad": 1,
        "precio_venta": "$12,500.00",
        "fecha_prometida": "05/04/2026",
        "fecha_entrega": None,
        "folio_factura": None,
    }
    base.update(kw)
    return [base[c.key] for c in ORDERS.columns]


def issues_by_row(parsed):
    return {(i.row, i.column): i.message for i in parsed.issues}


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_blank_templates_are_recognized(template):
    parsed = parse_file(f"{template.name}.xlsx", workbook_bytes(template))
    assert parsed.template is template
    assert parsed.issues == [] and parsed.data.empty


def test_a_filled_template_reads_back_what_was_written(demo_sample):
    rows = demo_sample.orders.head(20)
    parsed = parse_file("p.xlsx", workbook_bytes(ORDERS, rows))
    assert parsed.issues == []
    back = parsed.data.reset_index(drop=True)
    assert back["folio_pedido"].tolist() == rows["folio_pedido"].tolist()
    assert back["precio_venta"].tolist() == pytest.approx(rows["precio_venta"].tolist())
    assert back["fecha_prometida"].tolist() == rows["fecha_prometida"].tolist()


def test_headers_typed_by_hand_still_match():
    headers = [h.lower().replace("í", "i").replace("ó", "o") + " " for h in ORDER_HEADERS]
    parsed = parse_file("p.xlsx", xlsx(headers, [order_row()]))
    assert parsed.template is ORDERS and len(parsed.data) == 1


def test_an_unknown_file_says_what_it_is_missing():
    parsed = parse_file("ventas.xlsx", xlsx(["Folio", "Fecha", "Cliente"], [["1", "x", "y"]]))
    assert parsed.template is None
    msg = parsed.issues[0].message
    assert "Se parece a Cotizaciones" in msg and "Importe" in msg


def test_mexican_formats_are_understood():
    rows = [
        order_row(precio_venta="$12,500.00", fecha_pedido="15/03/2026"),
        order_row(partida=2, precio_venta="9 800 MXN", fecha_pedido="2026-03-15"),
        order_row(partida=3, precio_venta=7000, fecha_pedido=46096),  # Excel serial: 15 Mar 2026
    ]
    parsed = parse_file("p.xlsx", xlsx(ORDER_HEADERS, rows))
    assert parsed.issues == []
    assert parsed.data["precio_venta"].tolist() == [12500.0, 9800.0, 7000.0]
    assert set(parsed.data["fecha_pedido"].dt.date) == {date(2026, 3, 15)}


def test_each_mistake_is_reported_on_its_excel_row_and_the_rest_loads():
    rows = [
        order_row(),  # row 2: fine
        order_row(partida=2, fecha_pedido="31/02/2026"),  # row 3: no such date
        order_row(partida=3, precio_venta="pendiente"),  # row 4
        order_row(partida=4, cantidad=0),  # row 5
        order_row(partida=5, cliente=None),  # row 6
        order_row(partida=6, fecha_prometida="01/02/2026"),  # row 7: before the order
        order_row(partida=1),  # row 8: same key as row 2
        order_row(partida=7, precio_venta=-50),  # row 9
    ]
    parsed = parse_file("p.xlsx", xlsx(ORDER_HEADERS, rows))
    found = issues_by_row(parsed)
    assert "31/02/2026" in found[(3, "Fecha de pedido")]
    assert "no es un número" in found[(4, "Precio de venta")]
    assert "entero" in found[(5, "Cantidad")]
    assert "obligatoria" in found[(6, "Cliente")]
    assert "anterior a la fecha del pedido" in found[(7, "Fecha prometida")]
    assert "fila 2" in found[(8, "Folio de pedido + Partida")]
    assert "negativo" in found[(9, "Precio de venta")]
    assert parsed.data.index.tolist() == [2]
    assert parsed.rows_read == 8 and parsed.rows_rejected == 7


def test_choices_ignore_case_and_accents_but_not_spelling():
    headers = [c.label for c in QUOTES.columns]
    base = ["C-1", 1, "01/03/2026", "Cliente", "diseñador", "Ana", "Bruma", "Sofá", 1, 1000]
    rows = [
        base + ["ganada", "10/03/2026", None],
        [*base[:1], 2, *base[2:]] + ["Ganado", "10/03/2026", None],
    ]
    parsed = parse_file("c.xlsx", xlsx(headers, rows))
    assert parsed.data["estado"].tolist() == ["Ganada"]
    assert parsed.data["tipo_cliente"].tolist() == ["Diseñador"]
    assert "Opciones: Abierta, Ganada, Perdida" in parsed.issues[0].message


def test_a_closed_quote_without_close_date_is_kept_with_a_warning():
    headers = [c.label for c in QUOTES.columns]
    row = [
        "C-1",
        1,
        "01/03/2026",
        "Cliente",
        None,
        "Ana",
        "Bruma",
        "Sofá",
        1,
        1000,
        "Perdida",
        None,
        None,
    ]
    parsed = parse_file("c.xlsx", xlsx(headers, [row]))
    assert len(parsed.data) == 1
    assert [i.severity for i in parsed.issues] == ["aviso"]


def test_csv_in_latin1_with_semicolons():
    text = (
        ";".join(ORDER_HEADERS)
        + "\n"
        + ";".join("" if v is None else str(v) for v in order_row(precio_venta="1500"))
    )
    parsed = parse_file("pedidos.csv", text.encode("latin-1"))
    assert parsed.template is ORDERS and parsed.data["cliente"].tolist() == ["Estudio Alba"]


def test_unreadable_and_wrong_type_files_fail_politely():
    assert "No se pudo abrir" in parse_file("x.xlsx", b"not a workbook").issues[0].message
    assert ".xlsx o .csv" in parse_file("x.pdf", b"%PDF").issues[0].message


def test_the_demo_typos_are_all_caught(demo):
    errors = [i for i in demo.issues if i.severity == ERROR]
    assert {(i.file, i.column) for i in errors} == {
        ("cotizaciones.xlsx", "Estado"),
        ("pedidos.xlsx", "Precio de venta"),
        ("pedidos.xlsx", "Fecha prometida"),
    }
    orphan = [i for i in demo.issues if "P-99999" in i.message]
    assert orphan and orphan[0].file == "produccion.xlsx"


def test_parsed_dates_are_timestamps():
    parsed = parse_file("p.xlsx", xlsx(ORDER_HEADERS, [order_row()]))
    assert pd.api.types.is_datetime64_any_dtype(parsed.data["fecha_pedido"])
    assert date(1899, 12, 30) == ingest.EXCEL_EPOCH
