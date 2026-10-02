"""Detector and alerts on synthetic series where the right answer is known."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from bi_kpi import alerts, anomalies
from bi_kpi.anomalies import NATIONAL
from bi_kpi.config import load_settings
from bi_kpi.security import load_users

DAYS = pd.date_range("2018-01-01", "2018-06-30", freq="D")


def series(values, sid="orders", region=NATIONAL, dates=DAYS, den=None) -> pd.DataFrame:
    values = np.asarray(values, dtype=float)
    return pd.DataFrame(
        {
            "series": sid,
            "region": region,
            "date": dates[: len(values)],
            "num": values,
            "den": den if den is not None else np.full(len(values), 500.0),
            "value": values,
            "grain": "day",
        }
    )


def everything_complete(df) -> pd.Series:
    return pd.Series(True, index=pd.date_range("2017-01-01", "2019-01-01"))


def noisy(n, level=200.0, sd=8.0, seed=1):
    return level + np.random.default_rng(seed).normal(0, sd, n)


def alerts_of(det):
    return anomalies.episodes(det).query("alert")


def test_a_spike_is_an_alert_and_noise_is_not():
    v = noisy(len(DAYS))
    v[100] = 600  # Black Friday
    det = anomalies.detect(series(v), everything_complete(None))
    assert det.loc[det["date"] == DAYS[100], "z"].item() > 20
    sent = alerts_of(det)
    # Noise alone produces ~2 anomalous days a year but almost never an alert (measured:
    # 0.07 alerts per series-year); the persistence rule is what keeps it quiet.
    assert sent["start"].tolist() == [DAYS[100]]


def test_a_weekly_pattern_is_not_an_anomaly():
    """Sundays sell half: the weekday factor must absorb it."""
    v = noisy(len(DAYS))
    v[DAYS.dayofweek == 6] *= 0.5
    det = anomalies.detect(series(v), everything_complete(None))
    assert alerts_of(det).empty


def test_baseline_counts_observations_not_calendar_days():
    """The on-time series has no weekends: it must still be judged (it wasn't, once)."""
    business = DAYS[DAYS.dayofweek < 5]
    v = 0.93 + np.random.default_rng(2).normal(0, 0.01, len(business))
    v[80:] = 0.75  # deliveries collapse
    df = series(v, sid="on_time_delivery", dates=business)
    det = anomalies.detect(df, everything_complete(None))
    assert det.loc[det["date"] == business[80], "status"].item() == "anomaly_down"


def test_incomplete_and_thin_periods_are_never_flagged():
    v = noisy(len(DAYS))
    v[150:] = 20  # the data fades out
    usable = everything_complete(None)
    usable[usable.index >= DAYS[150]] = False
    den = np.full(len(DAYS), 500.0)
    den[120] = 5  # a day with 5 orders
    v[120] = 2000
    det = anomalies.detect(series(v, den=den), usable)
    assert set(det.loc[det["date"] >= DAYS[150], "status"]) == {"incomplete"}
    assert det.loc[det["date"] == DAYS[120], "status"].item() == "thin"
    assert alerts_of(det).empty


def test_data_cutoff_finds_the_fading_tail():
    v = pd.Series(noisy(len(DAYS)), index=DAYS)
    v.iloc[-10:] = [90, 80, 70, 60, 50, 40, 30, 20, 10, 5]
    assert anomalies.data_cutoff(v) == DAYS[-11]
    assert anomalies.data_cutoff(pd.Series(noisy(len(DAYS)), index=DAYS)) is None


def test_episodes_group_consecutive_days_and_respect_the_bad_direction():
    business = DAYS[DAYS.dayofweek < 5]
    v = 0.93 + np.random.default_rng(3).normal(0, 0.01, len(business))
    v[60:65] = 0.70  # five bad days: one episode
    v[90] = 1.0  # better than ever: not an alert for on-time delivery
    det = anomalies.detect(
        series(v, sid="on_time_delivery", dates=business), everything_complete(None)
    )
    eps = anomalies.episodes(det)
    down = eps[eps["direction"] == "down"]
    assert len(down) == 1 and down["points"].item() >= 4
    assert down["alert"].item()
    assert not eps[eps["direction"] == "up"]["alert"].any()


def ep(**kw):
    base = dict(
        series="orders",
        direction="up",
        severity="warning",
        points=1,
        grain="day",
        region=NATIONAL,
        start=DAYS[100],
        end=DAYS[100],
    )
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.parametrize(
    ("kw", "sent"),
    [
        ({}, False),  # one warning day: noted, not sent
        ({"points": 2}, True),  # it persisted
        ({"severity": "critical"}, True),  # |z| >= 6: at once
        ({"series": "on_time_delivery", "direction": "up", "severity": "critical"}, False),
        ({"series": "negative_reviews", "direction": "down", "points": 3}, False),
    ],
)
def test_alert_rule(kw, sent):
    assert anomalies.is_alert(ep(**kw)) is sent


def crafted(statuses, zs, grain="day"):
    return pd.DataFrame(
        {
            "series": "orders",
            "region": NATIONAL,
            "date": DAYS[: len(statuses)],
            "status": statuses,
            "z": zs,
            "grain": grain,
        }
    )


def test_alert_date_is_when_a_live_system_would_know():
    det = crafted(["normal", "anomaly_up", "anomaly_up", "anomaly_up"], [0, 4, 4.5, 7])
    e = ep(start=DAYS[1], end=DAYS[3], points=3)
    assert anomalies.alert_date(e, det) == DAYS[2]  # a warning is sent once it persists
    det.loc[1, "z"] = 8
    assert anomalies.alert_date(e, det) == DAYS[1]  # a critical one at once
    weekly = ep(start=DAYS[1], end=DAYS[3], points=3, grain="week")
    assert anomalies.alert_date(weekly, det) == DAYS[1] + pd.Timedelta(days=6)  # week close


def test_live_and_replay_agree():
    """Detecting with data only up to day D gives the verdicts the full history gives for D."""
    v = noisy(len(DAYS))
    v[100:103] = 260
    full = anomalies.detect(series(v), everything_complete(None))
    live = anomalies.detect(series(v[:102]), everything_complete(None))
    assert live["status"].tolist() == full[full["date"] <= DAYS[101]]["status"].tolist()


# --- routing and delivery ---------------------------------------------------------------------

USERS = load_users(load_settings().config_dir / "users.yaml")
CFG = alerts.load_alert_settings(load_settings().config_dir / "alerts.yaml", load_settings().root)


@pytest.mark.parametrize(
    ("region", "severity", "expected"),
    [
        (NATIONAL, "warning", ["direccion", "analista"]),
        ("Nordeste", "warning", ["gerente.nordeste"]),
        ("Nordeste", "critical", ["gerente.nordeste", "direccion"]),
        ("Sul", "warning", ["direccion", "analista"]),  # no Sul manager: nobody left out
    ],
)
def test_routing_follows_row_level_security(region, severity, expected):
    got = alerts.recipients(region, severity, USERS, CFG)
    assert got == expected
    assert not any(u.startswith("vendedor") for u in got)
