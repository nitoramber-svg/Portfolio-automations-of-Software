# 01 · BI & KPI Analytics — Olist e-commerce

[![CI](https://github.com/nitoramber-svg/Portfolio-automations-of-Software/actions/workflows/bi-kpi-analytics.yml/badge.svg)](https://github.com/nitoramber-svg/Portfolio-automations-of-Software/actions/workflows/bi-kpi-analytics.yml)

> 🚧 **Work in progress** — step 1 of 6 done (sources → staging → star schema). See the [design](docs/design.md).

An end-to-end Business Intelligence pipeline built on **real, public data from Olist**, a Brazilian
e-commerce marketplace (~100k orders, 2016–2018). It targets the day-to-day of a BI / Data Analyst role:
connect several sources, clean and model the data, define KPIs, build dashboards, and alert on anomalies.

*Pipeline de BI de punta a punta sobre datos reales y públicos de Olist. Diseñado a partir de una vacante
real de Analista de BI (Amazon Quick Suite, SQL, AWS). Ver el [diseño](docs/design.md) (en español).*

## What works today

| Area | Status |
|---|---|
| 3 source types: SQL database (SQLite as the transactional system, incremental extract), flat files (CSV), HTTP API (FX rates, with cache and fallback) | ✅ |
| Staging layer: typing, de-duplication, city normalization, category translation to Spanish | ✅ |
| Star schema: 6 dimensions, 3 fact tables, daily aggregate, BRL → MXN/USD conversion | ✅ |
| Tests on a hand-made fixture with known answers + CI | ✅ |
| Data quality checks and quarantine | ⏳ step 2 |
| KPI engine, targets, row-level security | ⏳ step 3 |
| Dashboard | ⏳ step 4 |
| Anomaly detection, alerts, event calendar, early warning | ⏳ step 5 |

## Quick start

```bash
pip install -e ".[dev]"

# Option A — synthetic data in the Olist schema (no account needed)
bi sample

# Option B — the real dataset
bi download                           # needs KAGGLE_USERNAME / KAGGLE_KEY
bi download --zip path/to/archive.zip # or the ZIP downloaded from Kaggle

bi load      # extract → staging → star schema  (add --offline to skip the FX API)
pytest
```

## Architecture

```
SQLite (orders, payments) ─┐
CSV files (catalog, ...)  ─┼─► raw ─► stg ─► mart (star schema) ─► KPIs ─► dashboard / alerts
FX API (BRL→MXN/USD)      ─┘                DuckDB
```

Transformations are plain, versioned SQL in [`sql/`](sql/), run in order by the pipeline.

## Data

*Brazilian E-Commerce Public Dataset by Olist* — published on
[Kaggle](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) under CC BY-NC-SA 4.0.
The data is not stored in this repository. `bi sample` generates **synthetic** data with the same schema
(clearly marked as such) so the project runs anywhere. This project is not affiliated with Olist.
