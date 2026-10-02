"""Command line: ``bi sample | download | load | quality | kpis | rls-export | dashboard |
run-daily | replay``."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from bi_kpi import alerts, events, kpis, pipeline, quality, sample, security
from bi_kpi.config import load_settings
from bi_kpi.download import DownloadError, download_from_kaggle, extract_zip


def _cmd_sample(args: argparse.Namespace) -> int:
    settings = load_settings()
    counts = sample.generate(settings.raw_dir, orders_per_day=args.orders_per_day, seed=args.seed)
    for table, n in counts.items():
        print(f"{table:22} {n:>9,}")
    print(f"Synthetic Olist-schema data written to {settings.raw_dir}")
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    settings = load_settings()
    try:
        if args.zip:
            written = extract_zip(Path(args.zip), settings.raw_dir)
        else:
            written = download_from_kaggle(settings.kaggle_dataset, settings.raw_dir)
    except DownloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    (settings.raw_dir / "SYNTHETIC_DATA.txt").unlink(missing_ok=True)
    print(f"{len(written)} Olist files in {settings.raw_dir}")
    return 0


def _print_quality(con: duckdb.DuckDBPyConnection, full: bool) -> None:
    rows = con.execute(
        "SELECT severity, check_id, rows, total, pct, orders, value_brl, description "
        "FROM quality.report" + ("" if full else " WHERE rows > 0")
    ).fetchall()
    for severity in quality.SEVERITIES:
        group = [r for r in rows if r[0] == severity]
        if not group:
            continue
        print(f"  [{severity}]")
        for _, check_id, n, total, pct, orders, value, description in group:
            money = f"  R$ {value:>12,.2f}" if orders else ""
            print(f"    {check_id:34} {n:>7,} / {total:<9,} {pct or 0:>7.3f}%{money}")
            if full:
                print(f"      {description}")


def _cmd_quality(args: argparse.Namespace) -> int:
    settings = load_settings()
    if not settings.warehouse.exists():
        print("error: no warehouse yet — run `bi load` first", file=sys.stderr)
        return 1
    with duckdb.connect(str(settings.warehouse), read_only=True) as con:
        print("Data quality (rows flagged / rows checked, value of the affected orders):")
        _print_quality(con, full=True)
        print("Reconciliation:")
        for name, expected, actual, ok in con.execute(
            "SELECT * FROM quality.reconciliation"
        ).fetchall():
            print(f"  {'OK ' if ok else 'BAD'} {name:50} {expected:>16,.2f} {actual:>16,.2f}")
    return 0


def _cmd_load(args: argparse.Namespace) -> int:
    settings = load_settings()
    try:
        report = pipeline.load(settings, offline_fx=args.offline, since=args.since)
    except quality.ReconciliationError as exc:
        print(
            f"error: the marts don't reconcile with staging, not publishing: {exc}", file=sys.stderr
        )
        return 1
    print("Extracted rows:")
    for table, n in report.extracted.items():
        print(f"  {table:22} {n:>9,}")
    print(f"FX source: {report.fx_source}")
    moved = ", ".join(f"{t} {n:,}" for t, n in report.quarantined.items() if n)
    print(f"Quarantined rows: {moved or 'none'}")
    with duckdb.connect(str(settings.warehouse), read_only=True) as con:
        print("Data quality (see `bi quality` for every check):")
        _print_quality(con, full=False)
    print("Star schema:")
    for table, n in report.marts.items():
        print(f"  mart.{table:22} {n:>9,}")
    print(f"Sales plan: {report.targets:,} region-months (config/targets.yaml)")
    print(
        f"Anomalies: {report.anomaly_episodes:,} episodes, {report.alerts:,} alerts "
        "(bi replay to see them)"
    )
    print(f"Warehouse: {settings.warehouse}")
    return 0


def _format(value: float, unit: str, currency: str) -> str:
    if value != value:  # NaN: not computable for this cut
        return "—"
    if unit == "currency":
        return f"{currency} {value:,.2f}"
    if unit == "ratio":
        return f"{value:.2%}"
    if unit == "count":
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def _cmd_kpis(args: argparse.Namespace) -> int:
    settings = load_settings()
    try:
        user = security.load_users(settings.config_dir / "users.yaml").get(args.user)
    except security.AccessDenied as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    catalog = kpis.load_kpis(settings.config_dir / "kpis.yaml")
    filters = dict(f.split("=", 1) for f in args.filter)
    with duckdb.connect(str(settings.warehouse), read_only=True) as con:
        df = kpis.compute(
            con,
            user,
            catalog,
            ids=args.kpi or None,
            start=date.fromisoformat(args.start) if args.start else None,
            end=date.fromisoformat(args.end) if args.end else None,
            filters=filters,
            currency=args.currency,
            by=args.by,
        )
    print(
        f"User {user.name} ({user.role}), {args.start or 'start'} → {args.end or 'end'}, "
        f"{args.currency.upper()}" + (f", {filters}" if filters else "")
    )
    marks = {"ok": "✓", "off_target": "✗"}
    for _, r in df.iterrows():
        key = f"{r['key']!s:>14}  " if args.by else ""
        target = (
            ""
            if pd.isna(r["target"])
            else f"  meta {_format(r['target'], r['unit'], args.currency.upper())}"
        )
        print(
            f"  {key}{marks.get(r['status'], ' ')} {r['name']:26} "
            f"{_format(r['value'], r['unit'], args.currency.upper()):>22}{target}"
        )
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    settings = load_settings()
    if not settings.warehouse.exists():
        print("error: no warehouse yet — run `bi load` first", file=sys.stderr)
        return 1
    app = Path(__file__).parent / "dashboard" / "app.py"
    # Always headless: otherwise Streamlit's first run stops at an e-mail sign-up prompt in
    # the terminal. We open the browser ourselves once the server answers.
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        "--server.port",
        str(args.port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    url = f"http://localhost:{args.port}"
    if not args.headless:
        threading.Thread(target=_open_when_up, args=(url,), daemon=True).start()
    print(f"Dashboard: {url}  (Ctrl+C to stop)")
    return subprocess.call(cmd)


def _open_when_up(url: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{url}/_stcore/health", timeout=2)
            webbrowser.open(url)
            return
        except OSError:
            time.sleep(0.5)


def _alert_settings(settings):
    return alerts.load_alert_settings(settings.config_dir / "alerts.yaml", settings.root)


def _print_alerts(df) -> None:
    for a in df.itertuples():
        to = ", ".join(a.recipients) or "—"
        print(f"  {a.sent_on}  {a.subject}")
        print(f"              → {to}")


def _cmd_run_daily(args: argparse.Namespace) -> int:
    """One day as a live system would run it: the alerts that day raises, delivered."""
    settings = load_settings()
    cfg = _alert_settings(settings)
    with duckdb.connect(str(settings.warehouse), read_only=True) as con:
        cutoff = con.execute("SELECT data_cutoff FROM alerts.meta").fetchone()[0]
        day = date.fromisoformat(args.date) if args.date else cutoff
        if cutoff is not None and day > cutoff:
            print(f"{day}: data incomplete after {cutoff} (end of the dataset) — no alerts")
            return 0
        todays = alerts.alerts_between(con, day, day)
        risk = alerts.at_risk_for(con, todays["alert_id"].tolist())
    if todays.empty:
        print(f"{day}: no alerts")
        return 0
    print(f"{day}: {len(todays)} alert(s)")
    _print_alerts(todays)
    for item in alerts.deliver(todays, cfg, risk):
        print(f"  delivered: {item}")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    """Walk a date range: which alerts would have arrived, when, and to whom."""
    settings = load_settings()
    cfg = _alert_settings(settings)
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    with duckdb.connect(str(settings.warehouse), read_only=True) as con:
        df = alerts.alerts_between(con, start, end)
        cutoff = con.execute("SELECT data_cutoff FROM alerts.meta").fetchone()[0]
        risk = alerts.at_risk_for(con, df["alert_id"].tolist()) if args.deliver else {}
    print(f"{len(df)} alerts between {start or 'start'} and {end or 'end'}")
    _print_alerts(df)
    if cutoff is not None:
        print(f"Data after {cutoff} is incomplete (end of the dataset): not judged, no alerts.")
    if len(df):
        per_month = df.assign(m=df["sent_on"].astype(str).str[:7]).groupby(["m", "name"]).size()
        print("Alerts per month and KPI:")
        print(per_month.unstack(fill_value=0).to_string())
    if args.deliver:
        done = alerts.deliver(df, cfg, risk)
        print(f"Delivered {len(done)} messages ({cfg.mode} mode, {cfg.outbox})")
    return 0


def _cmd_impact(args: argparse.Namespace) -> int:
    """What each registered event cost (config/events.yaml), against the detector's
    expectation without it."""
    settings = load_settings()
    with duckdb.connect(str(settings.warehouse), read_only=True) as con:
        df = events.impact(con)
    if df.empty:
        print("No events registered (config/events.yaml)")
        return 0
    kinds = {"planned": "planeado", "unplanned": "no planeado"}
    for r in df.itertuples():
        start, end = pd.Timestamp(r.start).date(), pd.Timestamp(r.end).date()
        print(f"{r.event} ({kinds[r.kind]}, {start} → {end})")
        if r.orders_expected:
            print(
                f"  pedidos: {r.orders:,.0f} vs {r.orders_expected:,.0f} esperados "
                f"({r.orders_delta_pct:+.0%})"
            )
        if pd.notna(r.pickups_delta_pct):
            print(f"  despachos a paquetería: {r.pickups_delta_pct:+.0%} vs lo esperado")
        if r.deliveries_due:
            print(
                f"  entregas tardías extra: {r.late_orders_extra:,.0f} de "
                f"{r.deliveries_due:,.0f} pedidos que vencían"
            )
        if r.score_before is not None and r.score_during is not None:
            print(
                f"  calificación: {r.score_before:.2f} → {r.score_during:.2f} "
                f"({r.score_during - r.score_before:+.2f})"
            )
    return 0


def _cmd_rls_export(args: argparse.Namespace) -> int:
    settings = load_settings()
    users = security.load_users(settings.config_dir / "users.yaml")
    path = security.export_rls_rules(users, Path(args.out))
    print(f"RLS rules for {len(users.by_name)} users written to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bi", description="BI & KPI analytics on Olist data")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sample", help="generate synthetic data in the Olist schema")
    p.add_argument("--orders-per-day", type=float, default=60.0)
    p.add_argument("--seed", type=int, default=42)
    p.set_defaults(func=_cmd_sample)

    p = sub.add_parser("download", help="get the real Olist data (Kaggle API or a local ZIP)")
    p.add_argument("--zip", help="path to the archive downloaded from Kaggle")
    p.set_defaults(func=_cmd_download)

    p = sub.add_parser("load", help="extract the sources and build the star schema")
    p.add_argument("--offline", action="store_true", help="do not call the FX API")
    p.add_argument("--since", help="only extract orders purchased on/after this date")
    p.set_defaults(func=_cmd_load)

    p = sub.add_parser("quality", help="data quality report and reconciliation of the last load")
    p.set_defaults(func=_cmd_quality)

    p = sub.add_parser("kpis", help="KPIs as one user sees them")
    p.add_argument("--user", default="direccion", help="a user from config/users.yaml")
    p.add_argument("--from", dest="start", help="first purchase date, YYYY-MM-DD")
    p.add_argument("--to", dest="end", help="last purchase date, YYYY-MM-DD")
    p.add_argument("--currency", default="BRL", help="BRL, MXN or USD")
    p.add_argument("--by", help="month, region, state, payment_type, category or seller")
    p.add_argument("--filter", action="append", default=[], help="e.g. region=Sul (repeatable)")
    p.add_argument("--kpi", action="append", help="only these KPI ids (repeatable)")
    p.set_defaults(func=_cmd_kpis)

    p = sub.add_parser("dashboard", help="open the Streamlit dashboard")
    p.add_argument("--port", type=int, default=8501)
    p.add_argument("--headless", action="store_true", help="don't open a browser")
    p.set_defaults(func=_cmd_dashboard)

    p = sub.add_parser("run-daily", help="deliver the alerts of one day, as a live run would")
    p.add_argument("--date", help="YYYY-MM-DD (default: the last complete day of data)")
    p.set_defaults(func=_cmd_run_daily)

    p = sub.add_parser("replay", help="which alerts would have arrived in a date range")
    p.add_argument("--from", dest="start", help="YYYY-MM-DD")
    p.add_argument("--to", dest="end", help="YYYY-MM-DD")
    p.add_argument("--deliver", action="store_true", help="also write/send them all")
    p.set_defaults(func=_cmd_replay)

    p = sub.add_parser("impact", help="what each registered event cost (config/events.yaml)")
    p.set_defaults(func=_cmd_impact)

    p = sub.add_parser("rls-export", help="write the RLS rules as a Quick Suite permissions file")
    p.add_argument("--out", default="data/rls_rules.csv")
    p.set_defaults(func=_cmd_rls_export)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
