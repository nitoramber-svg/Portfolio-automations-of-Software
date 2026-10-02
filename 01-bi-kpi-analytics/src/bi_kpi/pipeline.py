"""End-to-end load: extract the sources, build staging, check quality, build marts and plan."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb

from bi_kpi import alerts, anomalies, events, kpis, quality, security
from bi_kpi.config import Settings
from bi_kpi.extract import files, fx, oltp
from bi_kpi.olist import FILE_TABLES, FILES, OPTIONAL_TABLES
from bi_kpi.transform import ensure_schemas, load_seeds, run_sql

log = logging.getLogger(__name__)


@dataclass
class LoadReport:
    extracted: dict[str, int] = field(default_factory=dict)
    fx_source: str = ""
    sql_files: list[str] = field(default_factory=list)
    quarantined: dict[str, int] = field(default_factory=dict)
    marts: dict[str, int] = field(default_factory=dict)
    targets: int = 0
    anomaly_episodes: int = 0
    alerts: int = 0


def _purchase_range(con: duckdb.DuckDBPyConnection) -> tuple[date, date]:
    d0, d1 = con.execute(
        "SELECT min(TRY_CAST(order_purchase_timestamp AS DATE)), "
        "max(TRY_CAST(order_purchase_timestamp AS DATE)) FROM raw.orders"
    ).fetchone()
    if d0 is None:
        raise ValueError("raw.orders has no valid purchase dates")
    return d0, d1


def _swap_in(built: Path, target: Path, attempts: int = 10) -> None:
    """Replace the published warehouse with the new one in one step. Windows refuses while a
    reader holds the old file for a query (the dashboard opens it per query), so retry."""
    for i in range(attempts):
        try:
            os.replace(built, target)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.5)


def load(settings: Settings, offline_fx: bool = False, since: str | None = None) -> LoadReport:
    """Build everything into a new file and publish it only if every step succeeded: a load
    that fails half-way leaves the last good warehouse — and the dashboard — untouched."""
    report = LoadReport()
    settings.warehouse.parent.mkdir(parents=True, exist_ok=True)
    built = settings.warehouse.with_name(settings.warehouse.name + ".building")
    built.unlink(missing_ok=True)
    built.with_name(built.name + ".wal").unlink(missing_ok=True)
    try:
        _build(settings, built, report, offline_fx, since)
    except BaseException:
        built.unlink(missing_ok=True)
        raise
    _swap_in(built, settings.warehouse)
    return report


def _build(
    settings: Settings, path: Path, report: LoadReport, offline_fx: bool, since: str | None
) -> None:
    oltp.seed_oltp(settings.raw_dir, settings.oltp_db)

    with duckdb.connect(str(path)) as con:
        ensure_schemas(con)
        load_seeds(con, settings.seeds_dir)

        report.extracted.update(oltp.extract_oltp(con, settings.oltp_db, since=since))
        for table in FILE_TABLES:
            if table in OPTIONAL_TABLES and not (settings.raw_dir / FILES[table][0]).exists():
                log.info("optional source %s not present; left empty", table)
                files.create_empty(con, table)
                continue
            report.extracted[table] = files.load_csv(con, table, settings.raw_dir)

        start, end = _purchase_range(con)
        rates = fx.get_rates(settings.fx, start, end, offline=offline_fx)
        report.extracted["fx_rates"] = fx.load_rates(con, rates)
        report.fx_source = str(rates["fx_source"].iloc[0])

        report.sql_files = run_sql(con, settings.sql_dir, ("staging",))
        report.quarantined = quality.run(con)
        report.sql_files += run_sql(con, settings.sql_dir, ("marts",))
        quality.reconcile(con)
        calendar = events.load_events(settings.config_dir / "events.yaml")
        events.store(con, calendar)
        report.targets = kpis.build_targets(
            con,
            kpis.load_targets(settings.config_dir / "targets.yaml"),
            events.plan_uplifts(calendar),
        )
        report.anomaly_episodes = anomalies.run(con, calendar)["episodes"]
        report.alerts = alerts.build_log(
            con,
            security.load_users(settings.config_dir / "users.yaml"),
            alerts.load_alert_settings(settings.config_dir / "alerts.yaml", settings.root),
            alerts.load_playbooks(settings.config_dir / "playbooks.yaml"),
        )
        for (table,) in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'mart' AND table_type = 'BASE TABLE' ORDER BY table_name"
        ).fetchall():
            report.marts[table] = con.execute(f"SELECT count(*) FROM mart.{table}").fetchone()[0]
