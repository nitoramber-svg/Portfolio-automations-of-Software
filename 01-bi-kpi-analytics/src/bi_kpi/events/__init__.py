"""Event calendar (config/events.yaml): planned and unplanned events, and what they cost.

The detector reads it to leave event days out of its baselines and to stay quiet about what a
planned event is expected to move; the sales plan reads ``plan_uplift``; ``impact()`` compares
each event with what the detector expected without it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import yaml

KINDS = ("planned", "unplanned")


@dataclass(frozen=True)
class Event:
    name: str
    kind: str
    start: date
    end: date
    expected: tuple[str, ...] = ()
    plan_uplift: float = 0.0
    impact_lag_days: int = 14
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.name}: kind must be planned or unplanned")
        if self.end < self.start:
            raise ValueError(f"{self.name}: ends before it starts")
        if self.kind == "unplanned" and (self.expected or self.plan_uplift):
            raise ValueError(f"{self.name}: only planned events have expected/plan_uplift")

    def covers(self, day) -> bool:
        return self.start <= pd.Timestamp(day).date() <= self.end

    @property
    def impact_end(self) -> date:
        return self.end + timedelta(days=self.impact_lag_days)


def load_events(path: Path) -> tuple[Event, ...]:
    if not path.exists():
        return ()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return tuple(
        Event(
            name=e["name"],
            kind=e["kind"],
            start=pd.Timestamp(e["start"]).date(),
            end=pd.Timestamp(e["end"]).date(),
            expected=tuple(e.get("expected") or ()),
            plan_uplift=float(e.get("plan_uplift") or 0.0),
            impact_lag_days=int(e.get("impact_lag_days") or 14),
            note=" ".join(str(e.get("note") or "").split()),
        )
        for e in raw.get("events") or ()
    )


def event_days(events: tuple[Event, ...]) -> set[pd.Timestamp]:
    """Every day covered by an event: kept out of the baselines."""
    return {pd.Timestamp(d) for e in events for d in pd.date_range(e.start, e.end, freq="D")}


def plan_uplifts(events: tuple[Event, ...]) -> dict[int, float]:
    """year_month -> uplift of the sales plan (a planned event's month)."""
    return {
        e.start.year * 100 + e.start.month: e.plan_uplift
        for e in events
        if e.kind == "planned" and e.plan_uplift
    }


def store(con: duckdb.DuckDBPyConnection, events: tuple[Event, ...]) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS alerts")
    df = pd.DataFrame(
        [
            {
                "name": e.name,
                "kind": e.kind,
                "start": e.start,
                "end": e.end,
                "expected": list(e.expected),
                "plan_uplift": e.plan_uplift,
                "impact_end": e.impact_end,
                "note": e.note,
            }
            for e in events
        ],
        columns=["name", "kind", "start", "end", "expected", "plan_uplift", "impact_end", "note"],
    )
    con.register("_events", df)
    con.execute("CREATE OR REPLACE TABLE alerts.events AS SELECT * FROM _events")
    con.unregister("_events")


def impact(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Per event and national series: actual vs what the detector expected without it.

    Orders and pickups count over the event; on-time delivery over the deliveries due from the
    start to ``impact_end`` (they land later), as extra late orders; reviews over the same
    stretch, as the change in average score against the 8 weeks before. Expected values come
    from alerts.detections, whose baselines leave event days out.
    """
    events = con.execute("SELECT * FROM alerts.events ORDER BY start").df()
    rows = []
    for ev in events.itertuples():
        det = con.execute(
            "SELECT series, date, value, expected, den FROM alerts.detections "
            "WHERE region = 'Brasil' AND date BETWEEN ? AND ? AND expected IS NOT NULL",
            [ev.start, ev.impact_end],
        ).df()
        det["date"] = pd.to_datetime(det["date"])
        in_event = det["date"] <= pd.Timestamp(ev.end)

        def counts(sid: str, det=det, in_event=in_event) -> tuple[float, float]:
            d = det[(det["series"] == sid) & in_event].dropna(subset=["value", "expected"])
            return float(d["value"].sum()), float(d["expected"].sum())

        orders, orders_exp = counts("orders")
        pickups, pickups_exp = counts("carrier_pickups")
        due = det[det["series"] == "on_time_delivery"]
        late_extra = float(((due["expected"] - due["value"]) * due["den"]).sum())
        due_total = float(due["den"].sum())
        before, during = con.execute(
            """
            SELECT avg(score) FILTER (WHERE d < ?), avg(score) FILTER (WHERE d >= ?)
            FROM (SELECT CAST(strptime(CAST(created_date_key AS VARCHAR), '%Y%m%d') AS DATE) AS d,
                         score FROM mart.fact_reviews WHERE is_latest_for_order)
            WHERE d BETWEEN ? AND ?
            """,
            [ev.start, ev.start, ev.start - timedelta(days=56), ev.impact_end],
        ).fetchone()
        rows.append(
            {
                "event": ev.name,
                "kind": ev.kind,
                "start": ev.start,
                "end": ev.end,
                "orders": orders,
                "orders_expected": orders_exp,
                "orders_delta_pct": (orders / orders_exp - 1) if orders_exp else None,
                # No expectation yet (no history before the event): no number, not NaN.
                "pickups_delta_pct": (pickups / pickups_exp - 1) if pickups_exp > 0 else None,
                "late_orders_extra": late_extra,
                "deliveries_due": due_total,
                "score_before": before,
                "score_during": during,
            }
        )
    return pd.DataFrame(rows)
