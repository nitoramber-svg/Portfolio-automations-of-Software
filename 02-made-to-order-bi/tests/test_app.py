"""Every page renders for every role that has it, and the upload flow works end to end."""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from mto_bi import roles
from mto_bi.schema import ORDERS
from mto_bi.templates import workbook_bytes

PAGE = """
import sys; sys.path.insert(0, "src")
from mto_bi.app import main as m, pages
data, demo = m.current_dataset()
ctx = m.sidebar(data, demo)
pages.PAGES["{page}"](ctx)
"""
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CASES = [(r.key, p) for r in roles.ROLES for p in r.pages]


@pytest.mark.parametrize(("role", "page"), CASES, ids=[f"{r}-{p}" for r, p in CASES])
def test_page_renders(role, page):
    at = AppTest.from_string(PAGE.format(page=page), default_timeout=180)
    at.session_state["role"] = role
    at.run()
    assert not at.exception, at.exception[0].value


def test_the_shop_page_shows_no_money():
    at = AppTest.from_string(PAGE.format(page=roles.RISK), default_timeout=180)
    at.session_state["role"] = "taller"
    at.run()
    text = " ".join(str(m.value) for m in at.markdown) + " ".join(str(m.value) for m in at.metric)
    assert "$" not in text


def test_an_upload_with_errors_is_reported_and_can_be_applied(demo_sample):
    bad = demo_sample.orders.head(5).copy()
    bad.loc[bad.index[0], "cliente"] = None
    content = workbook_bytes(ORDERS, bad)
    script = PAGE.format(page=roles.UPLOAD)
    at = AppTest.from_string(script, default_timeout=180)
    at.session_state["role"] = "direccion"
    at.run()
    uploader = at.get("file_uploader")[0]
    if not hasattr(uploader, "set_value"):
        pytest.skip("this Streamlit's AppTest cannot upload files")
    uploader.set_value([("pedidos_nuevos.xlsx", content, XLSX)]).run()
    errors = " ".join(str(e.value) for e in at.error)
    assert "1 fila con error" in errors
    apply = next(b for b in at.button if b.label == "Usar estos datos")
    apply.click().run()
    assert not at.exception
    assert len(at.session_state["uploaded"].orders) == 4
