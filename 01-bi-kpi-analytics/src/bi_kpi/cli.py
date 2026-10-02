"""Command line: ``bi sample | download | load``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from bi_kpi import pipeline, sample
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


def _cmd_load(args: argparse.Namespace) -> int:
    settings = load_settings()
    report = pipeline.load(settings, offline_fx=args.offline, since=args.since)
    print("Extracted rows:")
    for table, n in report.extracted.items():
        print(f"  {table:22} {n:>9,}")
    print(f"FX source: {report.fx_source}")
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

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
