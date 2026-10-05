"""SAT invoices: both CFDI versions, ZIPs, foreign currency, and what is not a sale."""

from __future__ import annotations

import io
import zipfile

import pandas as pd
import pytest

from mto_bi import cfdi, metrics

UUID = "6F1E4F5A-1B2C-4D3E-9F8A-0123456789AB"


def invoice(
    version="4",
    kind="I",
    serie="A",
    folio="77",
    subtotal="1000.00",
    total="1160.00",
    currency="MXN",
    rate=None,
    uuid=UUID,
    stamped=True,
) -> bytes:
    rate_attr = f' TipoCambio="{rate}"' if rate else ""
    stamp = (
        f'<cfdi:Complemento><tfd:TimbreFiscalDigital Version="1.1" UUID="{uuid}"/></cfdi:Complemento>'
        if stamped
        else ""
    )
    return (
        f'<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/{version}" '
        'xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital" '
        f'Serie="{serie}" Folio="{folio}" Fecha="2026-03-15T10:30:00" SubTotal="{subtotal}" '
        f'Total="{total}" Moneda="{currency}"{rate_attr} TipoDeComprobante="{kind}">'
        '<cfdi:Emisor Rfc="EKU9003173C9"/><cfdi:Receptor Rfc="XAXX010101000" Nombre="Estudio Alba"/>'
        f"{stamp}</cfdi:Comprobante>"
    ).encode()


def test_cfdi_4_and_3_3_read_the_same():
    for version in ("4", "3"):
        inv = cfdi.parse_invoice(invoice(version=version))
        assert inv["uuid"] == UUID and inv["folio_key"] == "A77"
        assert inv["subtotal_mxn"] == 1000.0 and inv["fecha"] == pd.Timestamp("2026-03-15")


def test_dollars_are_converted_at_the_invoice_rate():
    inv = cfdi.parse_invoice(invoice(currency="USD", rate="18.50", subtotal="100", total="116"))
    assert inv["subtotal_mxn"] == 1850.0


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"<html></html>", "no es un CFDI"),
        (b"<<<", "no es un XML"),
        (invoice(stamped=False), "no está timbrada"),
        (b'<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>', "declaraciones"),
    ],
)
def test_what_is_not_a_valid_invoice_is_refused_with_a_reason(content, reason):
    with pytest.raises(ValueError, match=reason):
        cfdi.parse_invoice(content)


def test_a_sat_zip_payments_skipped_duplicates_once():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.xml", invoice())
        zf.writestr("a_copia.xml", invoice())  # same UUID
        zf.writestr("pago.xml", invoice(kind="P", uuid="11111111-1111-1111-1111-111111111111"))
        zf.writestr("leeme.txt", "not an invoice")
    batch = cfdi.read_invoices([("descarga_sat.zip", buf.getvalue())])
    assert len(batch.invoices) == 1 and batch.files_read == 3
    messages = " ".join(i.message for i in batch.issues)
    assert "1 comprobantes de tipo Pago" in messages and "repetida" in messages


def test_credit_notes_subtract_from_billing():
    batch = cfdi.read_invoices(
        [
            ("a.xml", invoice()),
            (
                "nc.xml",
                invoice(
                    kind="E",
                    folio="78",
                    subtotal="200",
                    total="232",
                    uuid="22222222-2222-2222-2222-222222222222",
                ),
            ),
        ]
    )
    assert metrics.billing_by_month(batch.invoices)["neto"].tolist() == [800.0]


def test_the_demo_invoices_all_parse(demo_sample):
    batch = cfdi.read_invoices(demo_sample.invoices)
    assert batch.files_read == len(demo_sample.invoices)
    assert not [i for i in batch.issues if i.severity == "error"]
