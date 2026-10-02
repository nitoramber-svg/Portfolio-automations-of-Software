"""Alerts: turn anomaly episodes into messages, route them like row-level security, deliver.

``build_log`` writes alerts.log: one row per alert, dated the day a live system would have sent
it (``anomalies.alert_date``), with its recipients and message. ``deliver`` writes them to the
outbox (demo) or sends them (live, config/alerts.yaml). ``bi run-daily`` delivers one day's
alerts; ``bi replay`` walks a date range and shows what would have arrived and when.
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


def message(ep) -> tuple[str, str]:
    """(subject, body) in Spanish, from an episode row."""
    s = SERIES_BY_ID[ep.series]
    where = "en todo Brasil" if ep.region == NATIONAL else f"en {ep.region}"
    verb = {"up": "sube", "down": "cae"}[ep.direction]
    icon = "🔴" if ep.severity == "critical" else "🟠"
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
    }[ep.series]
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
    lines.append("Severidad: " + ("crítica" if ep.severity == "critical" else "advertencia") + ".")
    return subject, "\n".join(lines)


def alert_id(ep) -> str:
    key = f"{ep.series}|{ep.region}|{ep.direction}|{pd.Timestamp(ep.start).date()}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def build_log(con: duckdb.DuckDBPyConnection, users: Users, cfg: AlertSettings) -> int:
    """alerts.log from alerts.episodes: the episodes that are alerts, with recipients."""
    eps = con.execute("SELECT * FROM alerts.episodes WHERE alert ORDER BY alert_date").df()
    rows = []
    for ep in eps.itertuples():
        subject, body = message(ep)
        rows.append(
            {
                "alert_id": alert_id(ep),
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
                "recipients": recipients(ep.region, ep.severity, users, cfg),
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
            " recipients VARCHAR[], subject VARCHAR, body VARCHAR)"
        )
    else:
        con.register("_log", df)
        con.execute("CREATE OR REPLACE TABLE alerts.log AS SELECT * FROM _log")
        con.unregister("_log")
    return len(df)


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


def _email(alert, user: str, to: str, cfg: AlertSettings) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM", "bi-alertas@example.com")
    msg["To"] = to
    msg["Subject"] = alert.subject
    link = f"{cfg.dashboard_url}/alertas?usuario={user}"
    msg.set_content(f"{alert.body}\n\nVer en el tablero: {link}\n")
    return msg


def _slack(alert, cfg: AlertSettings) -> dict:
    return {
        "channel": cfg.slack_channel,
        "text": alert.subject,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{alert.subject}*"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": alert.body}},
        ],
    }


def deliver(alerts: pd.DataFrame, cfg: AlertSettings) -> list[str]:
    """Send (live) or write to the outbox (demo) one e-mail per recipient and one Slack post
    per alert. Returns what was delivered, for the CLI."""
    done = []
    if cfg.mode == "demo":
        cfg.outbox.mkdir(parents=True, exist_ok=True)
    for alert in alerts.itertuples():
        stem = f"{pd.Timestamp(alert.sent_on).date()}_{alert.alert_id}"  # no ":" on Windows
        for user in alert.recipients:
            to = cfg.emails.get(user)
            if not to:
                logger.warning(
                    "no e-mail for %s; alert %s not mailed to them", user, alert.alert_id
                )
                continue
            msg = _email(alert, user, to, cfg)
            if cfg.mode == "demo":
                path = cfg.outbox / f"{stem}_{user}.eml"
                path.write_bytes(bytes(msg))
                done.append(str(path))
            else:
                _send_smtp(msg)
                done.append(f"mail → {to}")
        payload = _slack(alert, cfg)
        if cfg.mode == "demo":
            path = cfg.outbox / f"{stem}_slack.json"
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
