"""Flat-file source: Olist CSVs loaded as all-text raw tables (typing happens in staging)."""

from __future__ import annotations

from pathlib import Path

import duckdb

from bi_kpi.olist import FILES


class SourceContractError(ValueError):
    pass


def check_columns(csv_path: Path, required: list[str]) -> None:
    header = csv_path.open(encoding="utf-8-sig").readline().strip()
    columns = [c.strip().strip('"') for c in header.split(",")]
    missing = [c for c in required if c not in columns]
    if missing:
        raise SourceContractError(f"{csv_path.name} is missing columns {missing}")


def load_csv(con: duckdb.DuckDBPyConnection, table: str, raw_dir: Path) -> int:
    """Load one Olist CSV into ``raw.<table>`` with every column as VARCHAR."""
    file_name, required = FILES[table]
    path = raw_dir / file_name
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `bi download` or `bi sample` first")
    check_columns(path, required)
    con.execute(
        f"CREATE OR REPLACE TABLE raw.{table} AS "
        "SELECT * FROM read_csv(?, header = true, all_varchar = true, quote = '\"')",
        [str(path)],
    )
    return con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()[0]
