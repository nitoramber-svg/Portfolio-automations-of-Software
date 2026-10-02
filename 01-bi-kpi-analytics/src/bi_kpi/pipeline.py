"""End-to-end load: extract the sources, build staging, check quality, build marts and plan."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import duckdb

from bi_kpi import alerts, anomalies, kpis, quality, security
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


def load(settings: Settings, offline_fx: bool = False, since: str | None = None) -> LoadReport:
    report = LoadReport()
    settings.warehouse.parent.mkdir(parents=True, exist_ok=True)
    oltp.seed_oltp(settings.raw_dir, settings.oltp_db)

    with duckdb.connect(str(settings.warehouse)) as con:
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
        report.targets = kpis.build_targets(
            con, kpis.load_targets(settings.config_dir / "targets.yaml")
        )
        report.anomaly_episodes = anomalies.run(con)["episodes"]
        report.alerts = alerts.build_log(
            con,
            security.load_users(settings.config_dir / "users.yaml"),
            alerts.load_alert_settings(settings.config_dir / "alerts.yaml", settings.root),
        )
        for (table,) in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'mart' AND table_type = 'BASE TABLE' ORDER BY table_name"
        ).fetchall():
            report.marts[table] = con.execute(f"SELECT count(*) FROM mart.{table}").fetchone()[0]
    return report
