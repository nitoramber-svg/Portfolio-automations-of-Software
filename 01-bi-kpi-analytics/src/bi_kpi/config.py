"""Project paths and settings loaded from ``config/sources.yaml``."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(os.environ.get("BI_HOME", Path(__file__).resolve().parents[2]))


@dataclass(frozen=True)
class FxSettings:
    api_url: str
    base: str
    symbols: list[str]
    cache: Path
    fallback_rates: dict[str, float]


@dataclass(frozen=True)
class Settings:
    root: Path
    raw_dir: Path
    oltp_db: Path
    warehouse: Path
    kaggle_dataset: str
    fx: FxSettings
    sql_dir: Path = field(init=False)
    seeds_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sql_dir", self.root / "sql")
        object.__setattr__(self, "seeds_dir", self.root / "seeds")

    def with_data_dir(self, data_dir: Path) -> Settings:
        """Same settings, with every data file relocated under ``data_dir`` (used by tests)."""
        data_dir = Path(data_dir)
        return Settings(
            root=self.root,
            raw_dir=data_dir / "raw" / "olist",
            oltp_db=data_dir / "oltp.db",
            warehouse=data_dir / "warehouse.duckdb",
            kaggle_dataset=self.kaggle_dataset,
            fx=FxSettings(
                api_url=self.fx.api_url,
                base=self.fx.base,
                symbols=self.fx.symbols,
                cache=data_dir / "fx_rates.csv",
                fallback_rates=self.fx.fallback_rates,
            ),
        )


def load_settings(root: Path = PROJECT_ROOT) -> Settings:
    raw = yaml.safe_load((root / "config" / "sources.yaml").read_text(encoding="utf-8"))
    fx = raw["fx"]
    return Settings(
        root=root,
        raw_dir=root / raw["raw_dir"],
        oltp_db=root / raw["oltp_db"],
        warehouse=root / raw["warehouse"],
        kaggle_dataset=raw["kaggle_dataset"],
        fx=FxSettings(
            api_url=fx["api_url"],
            base=fx["base"],
            symbols=list(fx["symbols"]),
            cache=root / fx["cache"],
            fallback_rates={k: float(v) for k, v in fx["fallback_rates"].items()},
        ),
    )
