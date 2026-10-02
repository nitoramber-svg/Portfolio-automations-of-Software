from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
import pytest

from bi_kpi import pipeline
from bi_kpi.config import load_settings

FIXTURES = Path(__file__).parent / "fixtures" / "olist"


@pytest.fixture
def settings(tmp_path):
    s = load_settings().with_data_dir(tmp_path / "data")
    shutil.copytree(FIXTURES, s.raw_dir)
    return s


@pytest.fixture
def loaded(settings):
    """Warehouse built from the hand-made fixture, FX from the fallback rates."""
    report = pipeline.load(settings, offline_fx=True)
    con = duckdb.connect(str(settings.warehouse), read_only=True)
    yield report, con
    con.close()
