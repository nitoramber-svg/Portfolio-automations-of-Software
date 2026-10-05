"""Read a filled-in template (.xlsx or .csv), recognize which one it is, and validate it row by
row.

Rows with an error are left out and reported with their Excel row number, so whoever filled the
file can find and fix them; the rest of the file still loads. Warnings keep the row.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from mto_bi.schema import CHOICE, DATE, INT, MONEY, TEMPLATES, TEXT, Template, normalize

ERROR, WARNING = "error", "aviso"
EXCEL_EPOCH = date(1899, 12, 30)
DATE_FORMATS = ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y")


@dataclass(frozen=True)
class Issue:
    file: str
    row: int | None  # the row number as Excel shows it; None for the whole file
    column: str
    message: str
    severity: str = ERROR


@dataclass
class ParsedFile:
    name: str
    template: Template | None
    data: pd.DataFrame
    issues: list[Issue] = field(default_factory=list)
    rows_read: int = 0

    @property
    def rows_rejected(self) -> int:
        return len({i.row for i in self.issues if i.severity == ERROR and i.row is not None})


class UnreadableFile(ValueError):
    pass


def read_table(name: str, content: bytes) -> pd.DataFrame:
    """The first sheet of an .xlsx (or a .csv) as text and native cells, headers untouched."""
    lower = name.lower()
    try:
        if lower.endswith((".xlsx", ".xlsm")):
            return pd.read_excel(io.BytesIO(content), sheet_name=0, dtype=object)
        if lower.endswith(".csv"):
            for encoding in ("utf-8-sig", "latin-1"):
                try:
                    return pd.read_csv(
                        io.BytesIO(content), dtype=str, encoding=encoding, sep=None, engine="python"
                    )
                except UnicodeDecodeError:
                    continue
    except Exception as exc:  # a corrupt or password-protected workbook
        raise UnreadableFile(f"No se pudo abrir {name}: {exc}") from exc
    raise UnreadableFile(f"{name}: solo se aceptan archivos .xlsx o .csv")


def detect(headers) -> tuple[Template | None, dict[str, str]]:
    """Which template these headers belong to, and the mapping header -> column key.

    A header matches a column by its label or its key, ignoring accents, case and spaces. The
    template with the most matched columns wins, provided all its required columns are there.
    """
    best, best_map, best_hits = None, {}, 0
    for t in TEMPLATES:
        names = {}
        for c in t.columns:
            names[normalize(c.label)] = c.key
            names[normalize(c.key)] = c.key
        mapping = {}
        for h in headers:
            key = names.get(normalize(h))
            if key and key not in mapping.values():
                mapping[h] = key
        required = {c.key for c in t.columns if c.required}
        if required <= set(mapping.values()) and len(mapping) > best_hits:
            best, best_map, best_hits = t, mapping, len(mapping)
    return best, best_map


def closest_template(headers) -> tuple[Template, list[str]]:
    """For an unrecognized file: the template sharing most columns with it, and what it lacks."""
    got = {normalize(h) for h in headers}
    scored = []
    for t in TEMPLATES:
        present = [c for c in t.columns if normalize(c.label) in got or normalize(c.key) in got]
        missing = [c.label for c in t.columns if c.required and c not in present]
        scored.append((-len(present), len(missing), TEMPLATES.index(t), t, missing))
    *_, t, missing = min(scored, key=lambda s: s[:3])
    return t, missing


def parse_file(name: str, content: bytes) -> ParsedFile:
    try:
        raw = read_table(name, content)
    except UnreadableFile as exc:
        return ParsedFile(name, None, pd.DataFrame(), [Issue(name, None, "", str(exc))])
    raw = raw.dropna(how="all")
    template, mapping = detect(raw.columns)
    if template is None:
        t, missing = closest_template(raw.columns)
        msg = (
            f"No se reconoce como ninguna plantilla. Se parece a {t.label}, pero le faltan "
            f"estas columnas: {', '.join(missing)}. Revisa que la primera fila tenga los "
            "encabezados de la plantilla."
        )
        return ParsedFile(name, None, pd.DataFrame(), [Issue(name, None, "", msg)])
    return validate(name, template, raw.rename(columns=mapping))


def validate(name: str, template: Template, raw: pd.DataFrame) -> ParsedFile:
    issues: list[Issue] = []
    out = {}
    excel_rows = [int(i) + 2 for i in raw.index]  # header is row 1; pandas index 0 is row 2
    for col in template.columns:
        values = raw[col.key] if col.key in raw.columns else pd.Series([None] * len(raw))
        parsed = []
        for excel_row, value in zip(excel_rows, values, strict=True):
            result, problem = _parse(value, col)
            if problem:
                issues.append(Issue(name, excel_row, col.label, problem))
            elif result is None and col.required:
                issues.append(Issue(name, excel_row, col.label, "Está vacía y es obligatoria."))
            parsed.append(result)
        out[col.key] = parsed
    df = pd.DataFrame(out, index=excel_rows)
    for c in template.columns:
        if c.kind == DATE:
            df[c.key] = pd.to_datetime(df[c.key])
    issues += _rules(name, template, df)
    issues += _duplicates(name, template, df)

    bad = {i.row for i in issues if i.severity == ERROR}
    df = df[~df.index.isin(bad)]
    df.index.name = "fila"
    return ParsedFile(name, template, df, issues, rows_read=len(raw))


def _parse(value, col):
    """(value, None) or (None, problem). Empty cells come back as (None, None)."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NaT:
        return None, None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None, None
    if col.kind == TEXT:
        if isinstance(value, float) and value.is_integer():
            value = int(value)  # a folio typed as a number: 1234, not 1234.0
        if isinstance(value, datetime):
            value = value.date().isoformat()
        return str(value).strip(), None
    if col.kind == DATE:
        return _date(value)
    if col.kind in (INT, MONEY):
        number = _number(value)
        if number is None:
            return None, f"«{value}» no es un número."
        if col.kind == INT:
            if not float(number).is_integer() or number < 1:
                return None, f"«{value}» debe ser un número entero de 1 en adelante."
            return int(number), None
        if number < 0:
            return None, f"«{value}» es negativo."
        return round(float(number), 2), None
    if col.kind == CHOICE:
        wanted = normalize(value)
        for choice in col.choices:
            if normalize(choice) == wanted:
                return choice, None
        return None, f"«{value}» no es una opción. Opciones: {', '.join(col.choices)}."
    raise AssertionError(col.kind)


def _date(value):
    if isinstance(value, datetime):
        return value.date(), None
    if isinstance(value, date):
        return value, None
    if isinstance(value, int | float) and 20_000 < value < 80_000:  # an Excel serial date
        return EXCEL_EPOCH + timedelta(days=int(value)), None
    text = str(value).strip()
    text = re.sub(r"\s+\d{1,2}:\d{2}(:\d{2})?$", "", text)  # "15/03/2025 00:00"
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date(), None
        except ValueError:
            continue
    return None, f"«{value}» no es una fecha. Usa día/mes/año, por ejemplo 15/03/2025."


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    text = re.sub(r"[\s$]|MXN|MN", "", str(value), flags=re.IGNORECASE).replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _rules(name: str, t: Template, df: pd.DataFrame) -> list[Issue]:
    """Rules between columns of the same row."""
    out = []

    def check(mask, column, message, severity=ERROR):
        for row in df.index[mask.fillna(False)]:
            out.append(Issue(name, int(row), column, message, severity))

    if t.name == "cotizaciones":
        closed = df["estado"].isin(["Ganada", "Perdida"])
        check(
            closed & df["fecha_cierre"].isna(),
            "Fecha de cierre",
            "Falta la fecha de cierre: no cuenta para el tiempo de cierre.",
            WARNING,
        )
        check(
            df["fecha_cierre"] < df["fecha"],
            "Fecha de cierre",
            "Es anterior a la fecha de la cotización.",
        )
    elif t.name == "pedidos":
        check(
            df["fecha_prometida"] < df["fecha_pedido"],
            "Fecha prometida",
            "Es anterior a la fecha del pedido.",
        )
        check(
            df["fecha_entrega"] < df["fecha_pedido"],
            "Fecha de entrega",
            "Es anterior a la fecha del pedido.",
        )
    elif t.name == "produccion":
        check(df["fecha_fin"] < df["fecha_inicio"], "Fecha de fin", "Es anterior al inicio.")
    return out


def _duplicates(name: str, t: Template, df: pd.DataFrame) -> list[Issue]:
    if not t.key or df.empty:
        return []
    keys = df[list(t.key)].astype(str).apply(lambda r: "|".join(r).upper(), axis=1)
    first = {}
    out = []
    for row, k in keys.items():
        if k in first:
            label = " + ".join(t.column(c).label for c in t.key)
            out.append(Issue(name, int(row), label, f"Repetida: ya está en la fila {first[k]}."))
        else:
            first[k] = row
    return out
