"""Run the versioned SQL files (staging, then marts) against the warehouse."""

from __future__ import annotations

from pathlib import Path

import duckdb

SCHEMAS = ("raw", "seed", "stg", "mart")
LAYERS = ("staging", "marts")


def ensure_schemas(con: duckdb.DuckDBPyConnection) -> None:
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def load_seeds(con: duckdb.DuckDBPyConnection, seeds_dir: Path) -> list[str]:
    """Each ``seeds/<name>.csv`` becomes ``seed.<name>``."""
    names = []
    for path in sorted(seeds_dir.glob("*.csv")):
        con.execute(
            f"CREATE OR REPLACE TABLE seed.{path.stem} AS "
            "SELECT * FROM read_csv(?, header = true, delim = ',', quote = '\"')",
            [str(path)],
        )
        names.append(path.stem)
    return names


def sql_files(sql_dir: Path) -> list[Path]:
    return [p for layer in LAYERS for p in sorted((sql_dir / layer).glob("*.sql"))]


def run_sql(con: duckdb.DuckDBPyConnection, sql_dir: Path) -> list[str]:
    ran = []
    for path in sql_files(sql_dir):
        try:
            con.execute(path.read_text(encoding="utf-8"))
        except duckdb.Error as exc:
            raise RuntimeError(f"{path.relative_to(sql_dir)} failed: {exc}") from exc
        ran.append(f"{path.parent.name}/{path.name}")
    return ran
