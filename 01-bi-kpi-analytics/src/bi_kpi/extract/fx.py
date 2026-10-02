"""API source: daily BRL exchange rates from the Frankfurter API (ECB reference rates).

Resolution order: live API → local cache → configured fallback rates. The source used is
kept on every row (``fx_source``) so the dashboard can say how a number was converted.
"""

from __future__ import annotations

import logging
from datetime import date

import duckdb
import pandas as pd
import requests

from bi_kpi.config import FxSettings

log = logging.getLogger(__name__)


def fetch_rates(fx: FxSettings, start: date, end: date, timeout: int = 30) -> pd.DataFrame:
    url = f"{fx.api_url}/{start.isoformat()}..{end.isoformat()}"
    resp = requests.get(url, params={"from": fx.base, "to": ",".join(fx.symbols)}, timeout=timeout)
    resp.raise_for_status()
    rows = [
        {"rate_date": day, "currency": cur, "rate": rate}
        for day, rates in resp.json()["rates"].items()
        for cur, rate in rates.items()
    ]
    return pd.DataFrame(rows)


def get_rates(fx: FxSettings, start: date, end: date, offline: bool = False) -> pd.DataFrame:
    """Rates per (date, currency) with an ``fx_source`` column: api, cache or fallback."""
    if not offline:
        try:
            df = fetch_rates(fx, start, end)
            fx.cache.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(fx.cache, index=False)
            return df.assign(fx_source="api")
        except (requests.RequestException, KeyError, ValueError) as exc:
            log.warning("FX API unavailable (%s); trying cache", exc)
    if fx.cache.exists():
        df = pd.read_csv(fx.cache, dtype={"rate_date": str, "currency": str})
        if not df.empty:
            return df.assign(fx_source="cache")
    log.warning("No FX cache; using approximate fallback rates %s", fx.fallback_rates)
    return pd.DataFrame(
        [
            {"rate_date": start.isoformat(), "currency": cur, "rate": rate}
            for cur, rate in fx.fallback_rates.items()
        ]
    ).assign(fx_source="fallback")


def load_rates(con: duckdb.DuckDBPyConnection, rates: pd.DataFrame) -> int:
    con.register("_fx", rates)
    con.execute(
        "CREATE OR REPLACE TABLE raw.fx_rates AS "
        "SELECT CAST(rate_date AS VARCHAR) AS rate_date, currency, "
        "CAST(rate AS VARCHAR) AS rate, fx_source FROM _fx"
    )
    con.unregister("_fx")
    return len(rates)
