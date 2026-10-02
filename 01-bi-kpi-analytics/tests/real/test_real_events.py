"""Acceptance on the real Olist data (design §6): skipped where it isn't loaded, e.g. in CI.

The events the detector must find, the tail it must not mistake for a drop, and how noisy it
may be. Run after `bi load` with the real dataset.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd
import pytest

from bi_kpi.config import load_settings

SETTINGS = load_settings()


def _real_warehouse() -> bool:
    if not SETTINGS.warehouse.exists() or (SETTINGS.raw_dir / "SYNTHETIC_DATA.txt").exists():
        return False
    with duckdb.connect(str(SETTINGS.warehouse), read_only=True) as con:
        tables = {
            r[0]
            for r in con.execute(
                "SELECT table_schema || '.' || table_name FROM information_schema.tables"
            ).fetchall()
        }
        if "alerts.log" not in tables:
            return False
        return con.execute("SELECT count(*) FROM mart.fact_orders").fetchone()[0] > 90_000


pytestmark = pytest.mark.skipif(not _real_warehouse(), reason="real Olist data not loaded")


@pytest.fixture(scope="module")
def log():
    with duckdb.connect(str(SETTINGS.warehouse), read_only=True) as con:
        df = con.execute("SELECT * FROM alerts.log").df()
        cutoff = con.execute("SELECT data_cutoff FROM alerts.meta").fetchone()[0]
    df["sent_on"] = pd.to_datetime(df["sent_on"])
    return df, cutoff


def sent(df, series, start, end, direction=None):
    m = (df["series"] == series) & df["sent_on"].between(pd.Timestamp(start), pd.Timestamp(end))
    if direction:
        m &= df["direction"] == direction
    return df[m]


def test_black_friday_2017_is_detected(log):
    df, _ = log
    hit = sent(df, "orders", "2017-11-24", "2017-11-26", "up")
    assert (hit["region"] == "Brasil").any()
    assert (hit["severity"] == "critical").all()


def test_truckers_strike_2018_is_detected(log):
    """Strike 21–31 May 2018: orders due right after it arrive late, and orders drop."""
    df, _ = log
    late = sent(df, "on_time_delivery", "2018-05-28", "2018-06-10", "down")
    assert {"Brasil", "Sudeste"} <= set(late["region"])
    assert len(sent(df, "orders", "2018-05-21", "2018-06-03", "down")) >= 1


def test_end_of_the_data_is_incomplete_not_a_drop(log):
    df, cutoff = log
    assert cutoff is not None and date(2018, 8, 15) <= cutoff <= date(2018, 8, 31)
    assert df[df["sent_on"] > pd.Timestamp(cutoff)].empty


def test_unexplained_alerts_stay_under_one_per_kpi_per_month(log):
    """Alerts outside the known events, grouped per KPI and week (one event, several regions),
    average at most one per KPI per month over the complete months."""
    df, _ = log
    known = [
        ("2017-11-20", "2017-12-04"),  # Black Friday
        ("2017-12-04", "2018-01-05"),  # Christmas season
        ("2018-02-20", "2018-04-15"),  # March 2018 delivery crisis
        ("2018-05-20", "2018-06-15"),  # truckers' strike
    ]
    explained = pd.Series(False, index=df.index)
    for a, b in known:
        explained |= df["sent_on"].between(pd.Timestamp(a), pd.Timestamp(b))
    other = df[~explained]
    events = other.groupby(["series", other["sent_on"].dt.to_period("W")]).ngroups
    kpi_months = 4 * 20  # 4 series x Jan 2017 – Aug 2018
    assert events / kpi_months <= 1.0
