"""Command line: ``bi sample | download | load | quality``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

from bi_kpi import pipeline, quality, sample
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
    print(f"Warehouse: {settings.warehouse}")
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

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
