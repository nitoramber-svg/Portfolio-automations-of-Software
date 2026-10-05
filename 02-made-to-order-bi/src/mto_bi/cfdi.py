"""Read Mexican electronic invoices (CFDI 3.3 and 4.0 XML), one by one or in the ZIP that the
SAT's bulk download produces.

Every invoice in Mexico has this format, so this is the one source that needs no template.
Only income invoices (I) count as billing; credit notes (E) subtract; payment receipts (P),
transfers (T) and payroll (N) are skipped and reported.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import pandas as pd

from mto_bi.ingest import WARNING, Issue
from mto_bi.schema import folio_key

CFDI_NS = ("http://www.sat.gob.mx/cfd/4", "http://www.sat.gob.mx/cfd/3")
TFD_NS = "http://www.sat.gob.mx/TimbreFiscalDigital"
KINDS = {"I": "Ingreso", "E": "Egreso", "P": "Pago", "T": "Traslado", "N": "Nómina"}
MAX_ZIP_BYTES = 300 * 1024 * 1024
MAX_ZIP_FILES = 20_000
COLUMNS = [
    "uuid",
    "serie",
    "folio",
    "folio_key",
    "fecha",
    "tipo",
    "emisor_rfc",
    "receptor_rfc",
    "receptor_nombre",
    "subtotal_mxn",
    "total_mxn",
    "moneda",
    "archivo",
]


@dataclass
class InvoiceBatch:
    invoices: pd.DataFrame
    issues: list[Issue]
    files_read: int


def parse_invoice(content: bytes) -> dict:
    """One CFDI as a dict; raises ValueError with a readable reason."""
    if b"<!DOCTYPE" in content[:2048] or b"<!ENTITY" in content:
        raise ValueError("el XML trae declaraciones que una factura no lleva")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"no es un XML válido ({exc})") from exc
    ns = next((n for n in CFDI_NS if root.tag == f"{{{n}}}Comprobante"), None)
    if ns is None:
        raise ValueError("no es un CFDI (no tiene el nodo Comprobante del SAT)")
    a = root.attrib
    emisor = root.find(f"{{{ns}}}Emisor")
    receptor = root.find(f"{{{ns}}}Receptor")
    tfd = root.find(f".//{{{TFD_NS}}}TimbreFiscalDigital")
    if tfd is None:
        raise ValueError("no está timbrada (le falta el UUID del SAT)")
    currency = a.get("Moneda", "MXN")
    rate = float(a.get("TipoCambio") or 1) if currency not in ("MXN", "XXX") else 1.0
    discount = float(a.get("Descuento") or 0)
    serie, folio = a.get("Serie", ""), a.get("Folio", "")
    return {
        "uuid": tfd.attrib["UUID"].upper(),
        "serie": serie,
        "folio": folio,
        "folio_key": folio_key(serie + folio) if folio else "",
        "fecha": pd.Timestamp(a["Fecha"]).normalize(),
        "tipo": a.get("TipoDeComprobante", ""),
        "emisor_rfc": emisor.attrib.get("Rfc", "") if emisor is not None else "",
        "receptor_rfc": receptor.attrib.get("Rfc", "") if receptor is not None else "",
        "receptor_nombre": receptor.attrib.get("Nombre", "") if receptor is not None else "",
        "subtotal_mxn": round((float(a.get("SubTotal", 0)) - discount) * rate, 2),
        "total_mxn": round(float(a.get("Total", 0)) * rate, 2),
        "moneda": currency,
    }


def _expand(name: str, content: bytes, issues: list[Issue]):
    """(name, bytes) of each XML, opening ZIPs."""
    if name.lower().endswith(".zip"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile:
            issues.append(Issue(name, None, "", "El ZIP está dañado."))
            return
        members = [i for i in zf.infolist() if i.filename.lower().endswith(".xml")]
        if len(members) > MAX_ZIP_FILES or sum(i.file_size for i in members) > MAX_ZIP_BYTES:
            issues.append(Issue(name, None, "", "El ZIP es demasiado grande; divídelo en varios."))
            return
        for info in members:
            yield f"{name}/{info.filename}", zf.read(info)
    else:
        yield name, content


def read_invoices(files: list[tuple[str, bytes]]) -> InvoiceBatch:
    rows, issues, n = [], [], 0
    skipped: dict[str, int] = {}
    for name, content in files:
        for xml_name, xml in _expand(name, content, issues):
            n += 1
            try:
                inv = parse_invoice(xml)
            except ValueError as exc:
                issues.append(Issue(xml_name, None, "", f"No se leyó: {exc}."))
                continue
            if inv["tipo"] not in ("I", "E"):
                kind = KINDS.get(inv["tipo"], inv["tipo"])
                skipped[kind] = skipped.get(kind, 0) + 1
                continue
            inv["archivo"] = xml_name
            rows.append(inv)
    for kind, count in skipped.items():
        issues.append(
            Issue(
                "Facturas",
                None,
                "",
                f"Se omitieron {count} comprobantes de tipo {kind}: no son ventas.",
                WARNING,
            )
        )
    df = pd.DataFrame(rows, columns=COLUMNS)
    dup = df.duplicated("uuid", keep="first")
    for name in df.loc[dup, "archivo"]:
        issues.append(
            Issue(name, None, "", "Factura repetida (mismo UUID); se tomó una vez.", WARNING)
        )
    df = df[~dup].reset_index(drop=True)
    return InvoiceBatch(df, issues, n)
