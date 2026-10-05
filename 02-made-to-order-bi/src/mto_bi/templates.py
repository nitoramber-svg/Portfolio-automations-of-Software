"""Write the Excel templates: headers, drop-down lists, date formats and an instructions sheet.

The demo data is written through the same function, so the demo exercises exactly the files a
user fills in.
"""

from __future__ import annotations

import io
from datetime import date, datetime

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from mto_bi.schema import CHOICE, DATE, INT, MONEY, Template

HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
REQUIRED_FILL = PatternFill("solid", fgColor="FCE9C8")
VALIDATED_ROWS = 5000
KIND_LABELS = {
    "text": "Texto",
    "date": "Fecha (dd/mm/aaaa)",
    "int": "Número entero",
    "money": "Pesos, sin IVA",
    "choice": "Elegir de la lista",
}


def workbook_bytes(template: Template, rows: pd.DataFrame | None = None) -> bytes:
    """The template as an .xlsx file, optionally filled with ``rows`` (columns = keys)."""
    wb = Workbook()
    ws = wb.active
    ws.title = template.label
    for i, col in enumerate(template.columns, start=1):
        cell = ws.cell(row=1, column=i, value=col.label)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center")
        letter = get_column_letter(i)
        ws.column_dimensions[letter].width = max(14, len(col.label) + 4)
        if col.kind == CHOICE:
            dv = DataValidation(
                type="list",
                formula1='"' + ",".join(col.choices) + '"',
                allow_blank=not col.required,
                showErrorMessage=True,
                errorTitle="Valor no válido",
                error="Elige un valor de la lista: " + ", ".join(col.choices),
            )
            dv.add(f"{letter}2:{letter}{VALIDATED_ROWS}")
            ws.add_data_validation(dv)
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 22

    if rows is not None and len(rows):
        formats = {
            i: ("DD/MM/YYYY" if c.kind == DATE else "#,##0.00" if c.kind == MONEY else None)
            for i, c in enumerate(template.columns, start=1)
        }
        keys = [c.key for c in template.columns]
        for r, values in enumerate(rows.reindex(columns=keys).itertuples(index=False), start=2):
            for i, value in enumerate(values, start=1):
                value = _cell(value, template.columns[i - 1].kind)
                if value is None:
                    continue
                cell = ws.cell(row=r, column=i, value=value)
                if formats[i]:
                    cell.number_format = formats[i]

    _instructions(wb, template)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _cell(value, kind: str):
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NaT:
        return None
    if kind == DATE:
        if isinstance(value, pd.Timestamp):
            return value.to_pydatetime()
        if isinstance(value, date) and not isinstance(value, datetime):
            return datetime(value.year, value.month, value.day)
    try:
        if kind == INT:
            return int(value)
        if kind == MONEY:
            return float(value)
    except (TypeError, ValueError):
        return value  # written as typed: the upload report is what flags it
    return value


def _instructions(wb: Workbook, template: Template) -> None:
    ws = wb.create_sheet("Instrucciones")
    ws["A1"] = f"Plantilla de {template.label}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = template.purpose
    ws["A3"] = (
        "Una fila por pieza. Puedes pegar aquí lo que exportes de tu sistema, siempre que los "
        "encabezados de la primera hoja se queden igual. Las columnas en amarillo son "
        "obligatorias."
    )
    headers = ("Columna", "Obligatoria", "Tipo", "Qué poner", "Valores permitidos")
    for i, h in enumerate(headers, start=1):
        cell = ws.cell(row=5, column=i, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
    for r, col in enumerate(template.columns, start=6):
        ws.cell(row=r, column=1, value=col.label)
        ws.cell(row=r, column=2, value="Sí" if col.required else "No")
        ws.cell(row=r, column=3, value=KIND_LABELS[col.kind])
        ws.cell(row=r, column=4, value=col.help)
        ws.cell(row=r, column=5, value=", ".join(col.choices))
        if col.required:
            for c in range(1, 6):
                ws.cell(row=r, column=c).fill = REQUIRED_FILL
    for letter, width in zip("ABCDE", (22, 12, 20, 70, 60), strict=True):
        ws.column_dimensions[letter].width = width
    for row in ws.iter_rows(min_row=6, max_row=5 + len(template.columns)):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
