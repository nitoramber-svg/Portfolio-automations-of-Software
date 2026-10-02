"""Step 5b: event calendar, planned vs unplanned, plan uplift, playbooks, orders at risk,
digest delivery and the pipeline's resilience."""

from __future__ import annotations

import email
import email.policy
from datetime import date

import duckdb
import numpy as np
import pandas as pd
import pytest
import requests

from bi_kpi import alerts, anomalies, kpis, pipeline
from bi_kpi.anomalies import NATIONAL
from bi_kpi.config import load_settings
from bi_kpi.events import Event, load_events, plan_uplifts
from bi_kpi.extract import fx

DAYS = pd.date_range("2018-01-01", "2018-06-30", freq="D")
ALWAYS = pd.Series(True, index=pd.date_range("2017-01-01", "2019-01-01"))


def series(values, sid="orders"):
    values = np.asarray(values, dtype=float)
    return pd.DataFrame(
        {
            "series": sid,
            "region": NATIONAL,
            "date": DAYS[: len(values)],
            "num": values,
            "den": 500.0,
            "value": values,
            "grain": "day",
        }
    )


def noisy(n=None, level=200.0, sd=8.0, seed=1):
    return level + np.random.default_rng(seed).normal(0, sd, n or len(DAYS))


# --- calendar ----------------------------------------------------------------------------------


def test_the_repository_calendar_loads():
    cal = load_events(load_settings().config_dir / "events.yaml")
    names = {e.name: e for e in cal}
    assert names["Black Friday 2017"].kind == "planned"
    assert "orders" in names["Black Friday 2017"].expected
    assert names["Huelga de transportistas"].kind == "unplanned"
    assert plan_uplifts(cal) == {201711: 0.40}


@pytest.mark.parametrize(
    "kw",
    [
        {"kind": "maybe"},
        {"end": date(2018, 1, 1)},  # before the start
        {"kind": "unplanned", "expected": ("orders",)},  # only planned events expect things
    ],
)
def test_bad_events_are_refused(kw):
    base = dict(name="x", kind="planned", start=date(2018, 2, 1), end=date(2018, 2, 3))
    with pytest.raises(ValueError):
        Event(**{**base, **kw})


# --- detector ----------------------------------------------------------------------------------


def test_a_planned_event_is_marked_not_alerted_but_other_series_still_are():
    v = noisy()
    v[100] = 900  # Black Friday
    bf = Event("BF", "planned", DAYS[99].date(), DAYS[102].date(), expected=("orders",))
    det = anomalies.detect(series(v), ALWAYS, (bf,))
    assert det.loc[det["date"] == DAYS[100], "status"].item() == "planned"
    assert anomalies.episodes(det).query("alert").empty
    # The same spike on a series the event doesn't expect still alerts.
    det = anomalies.detect(series(v, sid="gmv"), ALWAYS, (bf,))
    assert det.loc[det["date"] == DAYS[100], "status"].item() == "anomaly_up"


def test_holidays_are_not_judged():
    v = noisy()
    v[100] = 0  # nobody ships on Corpus Christi
    det = anomalies.detect(series(v), ALWAYS, (), frozenset({DAYS[100]}))
    assert det.loc[det["date"] == DAYS[100], "status"].item() == "holiday"


def test_an_unplanned_event_stays_visible_while_it_lasts():
    """A long event must keep being judged against normal times, not against itself."""
    v = noisy()
    v[100:140] = 120  # a 40-day slump
    det_plain = anomalies.detect(series(v), ALWAYS)
    slump = Event("slump", "unplanned", DAYS[100].date(), DAYS[139].date())
    det_event = anomalies.detect(series(v), ALWAYS, (slump,))
    late = det_plain["date"] == DAYS[135]
    assert det_plain.loc[late, "status"].item() == "normal"  # the slump became the baseline
    assert det_event.loc[late, "status"].item() == "anomaly_down"  # still judged vs normal


def test_after_an_unplanned_event_its_days_count_again():
    v = noisy()
    v[100:110] = 120
    ev = Event("blip", "unplanned", DAYS[100].date(), DAYS[109].date())
    det = anomalies.detect(series(v), ALWAYS, (ev,))
    after = det[det["date"] == DAYS[115]]
    # The baseline after the event is the last 28 days, event included: no special casing.
    assert after["expected"].item() < 200


# --- plan --------------------------------------------------------------------------------------


def test_a_planned_month_raises_its_plan_and_does_not_inflate_the_next():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA mart")
    con.execute(
        "CREATE TABLE mart.dim_date AS SELECT CAST(d AS DATE) AS date, "
        "CAST(strftime(d, '%Y%m') AS INTEGER) AS year_month, TRUE AS is_complete_month "
        "FROM generate_series(TIMESTAMP '2017-08-01', TIMESTAMP '2018-02-28', INTERVAL 1 DAY) t(d)"
    )
    con.execute("CREATE TABLE mart.dim_region AS SELECT 'Sul' AS region")
    con.execute(
        "CREATE TABLE mart.v_sales (year_month INTEGER, customer_region VARCHAR, "
        "price_brl DECIMAL(12, 2), is_canceled BOOLEAN)"
    )
    for ym, gmv in {201708: 100, 201709: 100, 201710: 100, 201711: 140, 201712: 100}.items():
        con.execute("INSERT INTO mart.v_sales VALUES (?, 'Sul', ?, FALSE)", [ym, gmv])
    kpis.build_targets(con, kpis.TargetSettings(0.0, 3), {201711: 0.40})
    t = dict(con.execute("SELECT year_month, target_brl FROM mart.fact_targets").fetchall())
    assert float(t[201711]) == pytest.approx(140)  # 100 run rate x 1.40
    assert float(t[201712]) == pytest.approx(100)  # November counts as 140 / 1.40 = 100


# --- orders at risk, playbooks, digest --------------------------------------------------------


def test_orders_at_risk_are_what_was_open_that_day(loaded):
    _, con = loaded
    # On 7 March 2017 nothing had reached its customer yet: o1 arrived on the 8th, o5 on the
    # 15th, o2 on the 23rd and o4 never. o3 is canceled: not at risk, already lost.
    risk = alerts.orders_at_risk(con, date(2017, 3, 7), NATIONAL, horizon_days=30)
    assert set(risk["pedido"]) == {"o1", "o2", "o4", "o5"}
    on_the_9th = alerts.orders_at_risk(con, date(2017, 3, 9), NATIONAL, horizon_days=30)
    assert "o1" not in set(on_the_9th["pedido"])  # delivered on the 8th
    assert set(risk.columns) == {
        "pedido",
        "estado",
        "compra",
        "fecha_prometida",
        "situacion",
        "dias_para_vencer",
    }  # no customer id, no city
    within_a_week = alerts.orders_at_risk(con, date(2017, 3, 7), NATIONAL, horizon_days=7)
    assert within_a_week.empty  # nothing due before the 14th
    sudeste = alerts.orders_at_risk(con, date(2017, 3, 7), "Sudeste", horizon_days=30)
    assert set(sudeste["pedido"]) == {"o1", "o4"}  # c1 and c4 live in SP


def test_the_repository_playbooks_cover_every_alerting_direction():
    pbs = alerts.load_playbooks(load_settings().config_dir / "playbooks.yaml")
    for s in anomalies.SERIES:
        directions = ("up", "down") if s.alert_on == "both" else (s.alert_on,)
        for d in directions:
            assert pbs.get(s.id, d) is not None, (s.id, d)


def test_one_digest_per_person_per_day_with_the_lists_attached(tmp_path):
    cfg = alerts.AlertSettings(
        mode="demo",
        outbox=tmp_path / "outbox",
        dashboard_url="http://localhost:8501",
        slack_channel="#x",
        national=("direccion",),
        critical_copy=("direccion",),
        emails={"direccion": "direccion@example.com", "gerente.sul": "sul@example.com"},
    )
    rows = [
        {
            "alert_id": "a1",
            "sent_on": date(2018, 5, 27),
            "series": "carrier_pickups",
            "region": "Sul",
            "severity": "critical",
            "recipients": ["gerente.sul", "direccion"],
            "subject": "[BI] 🔴 ⏱ Despachos a paquetería cae en Sul: 138",
            "body": "uno",
        },
        {
            "alert_id": "a2",
            "sent_on": date(2018, 5, 27),
            "series": "orders",
            "region": NATIONAL,
            "severity": "warning",
            "recipients": ["direccion"],
            "subject": "[BI] 🟠 Pedidos cae en todo Brasil",
            "body": "dos",
        },
        {
            "alert_id": "a3",
            "sent_on": date(2018, 6, 1),
            "series": "orders",
            "region": NATIONAL,
            "severity": "warning",
            "recipients": ["direccion"],
            "subject": "[BI] 🟠 Otra",
            "body": "tres",
        },
    ]
    risk = {"a1": pd.DataFrame({"pedido": ["x1", "x2"], "estado": ["PR", "SC"]})}
    alerts.deliver(pd.DataFrame(rows), cfg, risk)
    names = sorted(p.name for p in cfg.outbox.iterdir())
    assert names == [
        "2018-05-27_direccion.eml",
        "2018-05-27_gerente.sul.eml",
        "2018-05-27_slack.json",
        "2018-06-01_direccion.eml",
        "2018-06-01_slack.json",
    ]

    def read(name):
        return email.message_from_bytes(
            (cfg.outbox / name).read_bytes(), policy=email.policy.default
        )

    boss = read("2018-05-27_direccion.eml")
    assert boss["Subject"] == "[BI] 🔴 2 alertas del dom 27-may-2018"
    text = boss.get_body(("plain",)).get_content()
    assert "uno" in text and "dos" in text
    attached = [p.get_filename() for p in boss.iter_attachments()]
    assert attached == ["pedidos_en_riesgo_carrier_pickups_Sul_a1.csv"]
    sul = read("2018-05-27_gerente.sul.eml")
    assert "dos" not in sul.get_body(("plain",)).get_content()  # not their alert


# --- resilience --------------------------------------------------------------------------------


def test_a_failed_load_leaves_the_last_good_warehouse(settings, monkeypatch):
    pipeline.load(settings, offline_fx=True)
    with duckdb.connect(str(settings.warehouse), read_only=True) as con:
        before = con.execute("SELECT count(*) FROM mart.fact_orders").fetchone()[0]

    def boom(*a, **k):
        raise RuntimeError("the anomaly step broke")

    monkeypatch.setattr(pipeline.anomalies, "run", boom)
    (settings.raw_dir / "olist_orders_dataset.csv").open("a", encoding="utf-8").write(
        "o9,c1,delivered,2017-03-09 10:00:00,2017-03-09 10:10:00,2017-03-10 09:00:00,"
        "2017-03-12 10:00:00,2017-03-25 00:00:00\n"
    )
    with pytest.raises(RuntimeError, match="anomaly step"):
        pipeline.load(settings, offline_fx=True)
    with duckdb.connect(str(settings.warehouse), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM mart.fact_orders").fetchone()[0] == before
    assert not list(settings.warehouse.parent.glob("*.building"))


def test_fx_api_is_retried_before_falling_back(settings, monkeypatch):
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise requests.ConnectionError("blip")
        return pd.DataFrame([{"rate_date": "2017-03-01", "currency": "MXN", "rate": 6.0}])

    monkeypatch.setattr(fx, "fetch_rates", flaky)
    monkeypatch.setattr(fx.time, "sleep", lambda s: None)
    rates = fx.get_rates(settings.fx, date(2017, 3, 1), date(2017, 3, 2))
    assert len(calls) == 3
    assert rates["fx_source"].iloc[0] == "api"
