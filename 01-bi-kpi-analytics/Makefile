.PHONY: install sample data load quality kpis dashboard screenshots replay impact export benchmark test lint

install:
	pip install -e ".[dev]"

sample:          ## Synthetic data in the Olist schema (no Kaggle account needed)
	bi sample

data:            ## Real Olist data (Kaggle API or ZIP=path/to/archive.zip)
	bi download $(if $(ZIP),--zip $(ZIP),)

load:            ## Extract the 3 sources, build staging and the star schema
	bi load

quality:         ## Report of every data quality check and the reconciliation
	bi quality

kpis:            ## KPIs as the director sees them (bi kpis --help for users and cuts)
	bi kpis

dashboard:       ## Streamlit dashboard on http://localhost:8501
	bi dashboard

screenshots:     ## Regenerate docs/screenshots with the Chrome/Edge already installed
	python scripts/screenshots.py

replay:          ## Every alert the history would have raised, and when
	bi replay

impact:          ## What each registered event cost (config/events.yaml)
	bi impact

export:          ## Parquet + Athena DDL + RLS rules for Quick Suite (data/export)
	bi export

benchmark:       ## Dashboard speed, views vs tables (docs/benchmark.md)
	python scripts/benchmark.py --compare-views

test:
	pytest

lint:
	ruff check src tests && ruff format --check src tests
