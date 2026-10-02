"""Acceptance on the real Olist data (design §6 and §6.1): skipped where it isn't loaded, e.g.
in CI. Run after `bi load` with the real dataset.

The events the detector must find, the one it must anticipate, the planned one it must not
alert on, the tail it must not mistake for a drop, and how noisy it may be.
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
def wh():
    with duckdb.connect(str(SETTINGS.warehouse), read_only=True) as con:
        log = con.execute("SELECT * FROM alerts.log").df()
        det = con.execute("SELECT * FROM alerts.detections WHERE region = 'Brasil'").df()
        cutoff = con.execute("SELECT data_cutoff FROM alerts.meta").fetchone()[0]
    log["sent_on"] = pd.to_datetime(log["sent_on"])
    det["date"] = pd.to_datetime(det["date"])
    return log, det, cutoff


def sent(log, series, start, end, region=None, direction=None):
    m = (log["series"] == series) & log["sent_on"].between(pd.Timestamp(start), pd.Timestamp(end))
    if region:
        m &= log["region"] == region
    if direction:
        m &= log["direction"] == direction
    return log[m]


def test_black_friday_is_detected_but_planned_not_alerted(wh):
    log, det, _ = wh
    day = det[(det["series"] == "orders") & (det["date"] == pd.Timestamp("2017-11-24"))]
    assert day["z"].item() > 20
    assert day["status"].item() == "planned"
    assert sent(log, "orders", "2017-11-20", "2017-12-03", direction="up").empty


def test_truckers_strike_is_anticipated(wh):
    """Strike 21–31 May 2018: carrier pickups fall days before on-time delivery does."""
    log, _, _ = wh
    early = sent(log, "carrier_pickups", "2018-05-21", "2018-06-03", "Brasil", "down")
    late = sent(log, "on_time_delivery", "2018-05-28", "2018-06-10", "Brasil", "down")
    assert len(early) and len(late)
    lead = (late["sent_on"].min() - early["sent_on"].min()).days
    assert lead >= 5
    assert (early["event"] == "Huelga de transportistas").all()


def test_march_2018_delivery_crisis_is_detected_within_days(wh):
    log, _, _ = wh
    hit = sent(log, "on_time_delivery", "2018-03-07", "2018-03-14", "Brasil", "down")
    assert len(hit)


def test_end_of_the_data_is_incomplete_not_a_drop(wh):
    log, _, cutoff = wh
    assert cutoff is not None and date(2018, 8, 15) <= cutoff <= date(2018, 8, 31)
    assert log[log["sent_on"] > pd.Timestamp(cutoff)].empty


def test_unexplained_alerts_stay_under_one_per_kpi_per_month(wh):
    """Alerts outside the registered events, grouped per series and week (one event, several
    regions), average at most one per series per month over the complete months."""
    log, det, _ = wh
    other = log[log["event"].isna()]
    events = other.groupby(["series", other["sent_on"].dt.to_period("W")]).ngroups
    months = 20  # Jan 2017 – Aug 2018
    assert events / (det["series"].nunique() * months) <= 1.0
