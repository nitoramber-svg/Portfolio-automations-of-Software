"""Every dashboard page renders for every role, on the fixture, without a browser.

Streamlit's AppTest runs the real script; the sidebar's user picker is driven like a person
would. The real sellers in config/users.yaml have no rows in the fixture, which is the point
of including one: every page must survive a user who can see nothing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from bi_kpi import pipeline
from bi_kpi.config import load_settings

FIXTURES = Path(__file__).parents[1] / "fixtures" / "olist"

PAGES = ("resumen", "ventas", "operacion", "alertas", "calidad")
USERS = ("direccion", "analista", "gerente.sudeste", "vendedor.4869f7a5")


def _page(path: str) -> None:
    from bi_kpi.dashboard import views

    views.render(path)


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    """One loaded fixture warehouse for the module; BI_DATA_DIR points the app at it."""
    data_dir = tmp_path_factory.mktemp("dash") / "data"
    settings = load_settings().with_data_dir(data_dir)
    shutil.copytree(FIXTURES, settings.raw_dir)
    pipeline.load(settings, offline_fx=True)
    mp = pytest.MonkeyPatch()
    mp.setenv("BI_DATA_DIR", str(data_dir))
    st.cache_resource.clear()
    st.cache_data.clear()
    yield data_dir
    mp.undo()
    st.cache_resource.clear()
    st.cache_data.clear()


def run(path: str, user: str) -> AppTest:
    at = AppTest.from_function(_page, args=(path,), default_timeout=60)
    at.run()
    at.sidebar.selectbox[0].select(user).run()
    return at


@pytest.mark.parametrize("user", USERS)
@pytest.mark.parametrize("path", PAGES)
def test_page_renders(warehouse, path, user):
    at = run(path, user)
    assert not at.exception, at.exception


def test_summary_shows_the_fixture_numbers(warehouse):
    at = run("resumen", "direccion")
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Ventas (GMV)"] == "R$ 310.00"
    assert metrics["Pedidos"] == "4"
    assert metrics["Entregas a tiempo (OTD)"] == "66.7%"
    reading = " ".join(m.value for m in at.markdown)
    assert r"Se vendieron R\$ 310.00 en 4 pedidos" in reading


def test_a_regional_manager_sees_only_their_region(warehouse):
    at = run("resumen", "gerente.sudeste")
    assert {m.label: m.value for m in at.metric}["Ventas (GMV)"] == "R$ 130.00"  # o1 + o4
    regions = at.sidebar.selectbox[1].options
    assert regions == ["Todas", "Sudeste"]


def test_quality_page_is_closed_to_regional_managers_and_sellers(warehouse):
    for user in ("gerente.sudeste", "vendedor.4869f7a5"):
        at = run("calidad", user)
        assert any("solo lo ven dirección y analistas" in w.value for w in at.warning)
        assert not at.dataframe
    at = run("calidad", "analista")
    assert at.dataframe


def test_currency_switch(warehouse):
    at = run("resumen", "direccion")
    at.sidebar.radio[0].set_value("MXN").run()
    assert {m.label: m.value for m in at.metric}["Ventas (GMV)"] == "MX$ 1,767.00"  # 310 x 5.7
