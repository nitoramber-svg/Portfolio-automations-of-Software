"""Alerts: turn anomaly episodes into messages, route them like row-level security, deliver.

``build_log`` writes alerts.log: one row per alert, dated the day a live system would have sent
it (``anomalies.alert_date``), with its recipients, message and playbook (config/playbooks.yaml),
and alerts.at_risk: the open orders each alert puts at risk. ``deliver`` sends one digest per
person per day — not one message per alert: the May 2018 strike raised 11 alerts in 12 days —
to the outbox (demo) or by e-mail and Slack (live, config/alerts.yaml). ``bi run-daily``
delivers one day; ``bi replay`` walks a date range and shows what would have arrived and when.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import smtplib
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import duckdb
import pandas as pd
import yaml

from bi_kpi.anomalies import NATIONAL, SERIES_BY_ID
from bi_kpi.security import Users

logger = logging.getLogger(__name__)
MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
DAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


@dataclass(frozen=True)
class AlertSettings:
    mode: str
    outbox: Path
    dashboard_url: str
    slack_channel: str
    national: tuple[str, ...]
    critical_copy: tuple[str, ...]
    emails: dict[str, str] = field(default_factory=dict)


def load_alert_settings(path: Path, root: Path) -> AlertSettings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw["mode"] not in ("demo", "live"):
        raise ValueError("alerts.yaml: mode must be demo or live")
    return AlertSettings(
        mode=raw["mode"],
        outbox=root / raw["outbox"],
        dashboard_url=raw["dashboard_url"].rstrip("/"),
        slack_channel=raw["slack_channel"],
        national=tuple(raw["national"]),
        critical_copy=tuple(raw["critical_copy"]),
        emails=dict(raw.get("emails") or {}),
    )


@dataclass(frozen=True)
class Playbook:
    owner: str
    actions: tuple[str, ...]
    orders_at_risk: bool = False


@dataclass(frozen=True)
class Playbooks:
    horizon_days: int = 7
    by_key: dict[tuple[str, str], Playbook] = field(default_factory=dict)

    def get(self, series: str, direction: str) -> Playbook | None:
        return self.by_key.get((series, direction))


def load_playbooks(path: Path) -> Playbooks:
    if not path.exists():
        return Playbooks()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Playbooks(
        horizon_days=int(raw.get("horizon_days", 7)),
        by_key={
            (series, direction): Playbook(
                owner=spec["owner"],
                actions=tuple(spec.get("actions") or ()),
                orders_at_risk=bool(spec.get("orders_at_risk", False)),
            )
            for series, directions in (raw.get("playbooks") or {}).items()
            for direction, spec in directions.items()
        },
    )


def orders_at_risk(
    con: duckdb.DuckDBPyConnection, day: date, region: str, horizon_days: int
) -> pd.DataFrame:
    """Orders open on ``day`` (bought, not canceled, not yet delivered) in ``region``, due within
    ``horizon_days`` or already overdue — what is known on that day, nothing later. Order id,
    customer state and dates only: the list goes by e-mail."""
    region_filter = "" if region == NATIONAL else "AND c.region = ?"
    params = [day, day, day, day, day, horizon_days] + ([] if region == NATIONAL else [region])
    return con.execute(
        f"""
        SELECT o.order_id                                  AS pedido,
               c.state                                     AS estado,
               CAST(o.purchased_at AS DATE)                AS compra,
               o.estimated_delivery                        AS fecha_prometida,
               CASE WHEN o.shipped_at IS NULL OR CAST(o.shipped_at AS DATE) > ?
                    THEN 'sin despachar' ELSE 'en tránsito' END AS situacion,
               date_diff('day', CAST(? AS DATE), o.estimated_delivery) AS dias_para_vencer
        FROM mart.fact_orders o JOIN mart.dim_customer c USING (customer_key)
        WHERE NOT o.is_canceled
          AND CAST(o.purchased_at AS DATE) <= ?
          AND (o.delivered_at IS NULL OR CAST(o.delivered_at AS DATE) > ?)
          AND o.estimated_delivery <= CAST(? AS DATE) + ?
          {region_filter}
        ORDER BY o.estimated_delivery, o.order_id
        """,
        params,
    ).df()


def recipients(region: str, severity: str, users: Users, cfg: AlertSettings) -> list[str]:
    """Who gets an alert: national ones to `national`; a region's to its managers (and to
    `critical_copy` when critical), or to `national` when the region has no manager, so no
    alert goes to nobody. Sellers never: alerts are per region."""
    if region == NATIONAL:
        names = list(cfg.national)
    else:
        names = [
            u.name
            for u in users.by_name.values()
            if u.role == "regional_manager" and region in u.regions
        ]
        if not names:
            names = list(cfg.national)
        elif severity == "critical":
            names += [n for n in cfg.critical_copy if n not in names]
    return [n for n in names if n in users.by_name]


def _day(d) -> str:
    d = pd.Timestamp(d)
    return f"{DAYS[d.dayofweek]} {d.day}-{MONTHS[d.month - 1]}-{d.year}"


def _fmt(value: float, unit: str) -> str:
    if pd.isna(value):
        return "—"
    if unit == "ratio":
        return f"{value:.1%}"
    if unit == "currency":
        return f"R$ {value:,.0f}"
    return f"{value:,.0f}"


def message(ep, playbook: Playbook | None = None, at_risk: int | None = None) -> tuple[str, str]:
    """(subject, body) in Spanish, from an episode row, with what to do about it."""
    s = SERIES_BY_ID[ep.series]
    where = "en todo Brasil" if ep.region == NATIONAL else f"en {ep.region}"
    verb = {"up": "sube", "down": "cae"}[ep.direction]
    icon = "🔴" if ep.severity == "critical" else "🟠"
    if s.leading:
        icon += " ⏱"
    period = f"semana del {_day(ep.start)}" if ep.grain == "week" else _day(ep.start)
    subject = (
        f"[BI] {icon} {s.name} {verb} {where}: {_fmt(ep.value, s.unit)} "
        f"(esperado {_fmt(ep.expected, s.unit)})"
    )
    dated = {
        "orders": "por fecha de compra",
        "gmv": "por fecha de compra",
        "on_time_delivery": "de los pedidos que debían llegar ese día",
        "negative_reviews": "por fecha de la reseña",
        "carrier_pickups": "pedidos entregados a la paquetería ese día",
        "late_dispatch": "de los que debían despacharse ese día, los que no salieron",
    }[ep.series]
    if ep.grain == "week":
        dated = dated.replace("ese día", "esa semana")
    lines = [
        f"{s.name} {where}, {period}: {_fmt(ep.value, s.unit)} ({dated}).",
        f"Lo esperado según los periodos anteriores: {_fmt(ep.expected, s.unit)} "
        f"(z = {ep.z:+.1f}; se alerta con |z| > 3.5).",
    ]
    if ep.points > 1:
        lines.append(
            f"Lleva {ep.points} periodos así ({_day(ep.start)} → {_day(ep.end)}); el peor fue "
            f"{_day(ep.peak_date)}: {_fmt(ep.peak_value, s.unit)} (z = {ep.peak_z:+.1f})."
        )
    if s.leading:
        lines.append(
            "Alerta temprana: este indicador se mueve antes que las entregas a tiempo "
            "(en la huelga de 2018, 7 días antes)."
        )
    event = getattr(ep, "event", None)
    if isinstance(event, str) and event:
        lines.append(f"Evento registrado en esas fechas: {event}.")
    lines.append("Severidad: " + ("crítica" if ep.severity == "critical" else "advertencia") + ".")
    if playbook:
        lines.append(f"Qué hacer — responsable: {playbook.owner}")
        lines += [f"  {i}. {a}" for i, a in enumerate(playbook.actions, 1)]
    if at_risk is not None:
        lines.append(
            f"Pedidos en riesgo: {at_risk:,} abiertos que vencen en 7 días o ya vencieron "
            "(lista adjunta)."
        )
    return subject, "\n".join(lines)


def alert_id(ep) -> str:
    key = f"{ep.series}|{ep.region}|{ep.direction}|{pd.Timestamp(ep.start).date()}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def build_log(
    con: duckdb.DuckDBPyConnection,
    users: Users,
    cfg: AlertSettings,
    playbooks: Playbooks | None = None,
) -> int:
    """alerts.log from alerts.episodes (the episodes that are alerts, with recipients and
    playbook) and alerts.at_risk (the orders each alert attaches)."""
    playbooks = playbooks or Playbooks()
    eps = con.execute("SELECT * FROM alerts.episodes WHERE alert ORDER BY alert_date").df()
    rows, risk = [], []
    for ep in eps.itertuples():
        pb = playbooks.get(ep.series, ep.direction)
        aid = alert_id(ep)
        n_risk = None
        if pb and pb.orders_at_risk:
            day = pd.Timestamp(ep.alert_date).date()
            orders = orders_at_risk(con, day, ep.region, playbooks.horizon_days)
            risk.append(orders.assign(alert_id=aid))
            n_risk = len(orders)
        subject, body = message(ep, pb, n_risk)
        rows.append(
            {
                "alert_id": aid,
                "sent_on": pd.Timestamp(ep.alert_date).date(),
                "series": ep.series,
                "name": SERIES_BY_ID[ep.series].name,
                "region": ep.region,
                "direction": ep.direction,
                "severity": ep.severity,
                "start": pd.Timestamp(ep.start).date(),
                "end": pd.Timestamp(ep.end).date(),
                "value": ep.value,
                "expected": ep.expected,
                "z": ep.z,
                "peak_z": ep.peak_z,
                "is_leading": SERIES_BY_ID[ep.series].leading,
                "event": ep.event if isinstance(getattr(ep, "event", None), str) else None,
                "recipients": recipients(ep.region, ep.severity, users, cfg),
                "owner": pb.owner if pb else None,
                "at_risk": n_risk,
                "subject": subject,
                "body": body,
            }
        )
    df = pd.DataFrame(rows)
    con.execute("CREATE SCHEMA IF NOT EXISTS alerts")
    if df.empty:
        con.execute(
            "CREATE OR REPLACE TABLE alerts.log (alert_id VARCHAR, sent_on DATE, series VARCHAR,"
            " name VARCHAR, region VARCHAR, direction VARCHAR, severity VARCHAR, start DATE,"
            ' "end" DATE, value DOUBLE, expected DOUBLE, z DOUBLE, peak_z DOUBLE,'
            " is_leading BOOLEAN, event VARCHAR, recipients VARCHAR[], owner VARCHAR,"
            " at_risk BIGINT, subject VARCHAR, body VARCHAR)"
        )
    else:
        con.register("_log", df)
        con.execute("CREATE OR REPLACE TABLE alerts.log AS SELECT * FROM _log")
        con.unregister("_log")
    risk_df = (
        pd.concat(risk, ignore_index=True)
        if risk
        else pd.DataFrame(
            columns=[
                "pedido",
                "estado",
                "compra",
                "fecha_prometida",
                "situacion",
                "dias_para_vencer",
                "alert_id",
            ]
        )
    )
    con.register("_risk", risk_df)
    con.execute("CREATE OR REPLACE TABLE alerts.at_risk AS SELECT * FROM _risk")
    con.unregister("_risk")
    return len(df)


def at_risk_for(con, alert_ids: list[str]) -> dict[str, pd.DataFrame]:
    """alert_id -> its orders at risk (for the attachments)."""
    if not alert_ids:
        return {}
    marks = ", ".join("?" for _ in alert_ids)
    df = con.execute(f"SELECT * FROM alerts.at_risk WHERE alert_id IN ({marks})", alert_ids).df()
    return {aid: g.drop(columns="alert_id") for aid, g in df.groupby("alert_id")}


def alerts_between(con, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    clauses, params = [], []
    if start:
        clauses.append("sent_on >= ?")
        params.append(start)
    if end:
        clauses.append("sent_on <= ?")
        params.append(end)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    df = con.execute(f"SELECT * FROM alerts.log{where} ORDER BY sent_on, region", params).df()
    # DuckDB hands DATE back as a timestamp; alerts are per day.
    df["sent_on"] = pd.to_datetime(df["sent_on"]).dt.date
    return df


def _digest_subject(day, items) -> str:
    if len(items) == 1:
        return items[0].subject
    worst = "🔴" if any(a.severity == "critical" for a in items) else "🟠"
    return f"[BI] {worst} {len(items)} alertas del {_day(day)}"


def _email(day, items, user: str, to: str, cfg: AlertSettings, risk) -> EmailMessage:
    """One e-mail for one person and day: every alert of theirs, each with its playbook and
    its list of orders at risk attached as CSV."""
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM", "bi-alertas@example.com")
    msg["To"] = to
    msg["Subject"] = _digest_subject(day, items)
    link = f"{cfg.dashboard_url}/alertas?usuario={user}"
    parts = [f"{a.subject.removeprefix('[BI] ')}\n{'-' * 60}\n{a.body}" for a in items]
    msg.set_content("\n\n".join(parts) + f"\n\nVer en el tablero: {link}\n")
    for a in items:
        orders = risk.get(a.alert_id)
        if orders is not None and len(orders):
            msg.add_attachment(
                orders.to_csv(index=False).encode("utf-8"),
                maintype="text",
                subtype="csv",
                filename=f"pedidos_en_riesgo_{a.series}_{a.region}_{a.alert_id}.csv",
            )
    return msg


def _slack(day, items, cfg: AlertSettings) -> dict:
    """One Slack post per day, one line per alert."""
    lines = "\n".join(f"• {a.subject.removeprefix('[BI] ')}" for a in items)
    return {
        "channel": cfg.slack_channel,
        "text": _digest_subject(day, items),
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{_digest_subject(day, items)}*"},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": lines}},
        ],
    }


def deliver(
    alerts: pd.DataFrame, cfg: AlertSettings, risk: dict[str, pd.DataFrame] | None = None
) -> list[str]:
    """Send (live) or write to the outbox (demo) one digest e-mail per person and day and one
    Slack post per day. Returns what was delivered, for the CLI."""
    risk = risk or {}
    done = []
    if cfg.mode == "demo":
        cfg.outbox.mkdir(parents=True, exist_ok=True)
    for day, today in alerts.groupby(alerts["sent_on"].map(lambda d: pd.Timestamp(d).date())):
        items = list(today.itertuples())
        people = dict.fromkeys(u for a in items for u in a.recipients)
        for user in people:
            to = cfg.emails.get(user)
            if not to:
                logger.warning("no e-mail for %s; their %s alerts not mailed", user, day)
                continue
            mine = [a for a in items if user in a.recipients]
            msg = _email(day, mine, user, to, cfg, risk)
            if cfg.mode == "demo":
                path = cfg.outbox / f"{day}_{user}.eml"  # no ":" in names: Windows
                path.write_bytes(bytes(msg))
                done.append(str(path))
            else:
                _send_smtp(msg)
                done.append(f"mail → {to}")
        payload = _slack(day, items, cfg)
        if cfg.mode == "demo":
            path = cfg.outbox / f"{day}_slack.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            done.append(str(path))
        else:
            _send_slack(payload)
            done.append(f"slack → {cfg.slack_channel}")
    return done


def _send_smtp(msg: EmailMessage) -> None:  # pragma: no cover - needs a server
    host = os.environ.get("SMTP_HOST")
    if not host:
        logger.warning("SMTP_HOST not set: e-mail skipped")
        return
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as smtp:
        smtp.starttls()
        if os.environ.get("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(msg)


def _send_slack(payload: dict) -> None:  # pragma: no cover - needs a webhook
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        logger.warning("SLACK_WEBHOOK_URL not set: Slack skipped")
        return
    req = urllib.request.Request(
        url, json.dumps(payload).encode(), {"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=30)
