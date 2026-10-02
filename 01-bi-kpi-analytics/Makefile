.PHONY: install sample data load quality test lint

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

test:
	pytest

lint:
	ruff check src tests && ruff format --check src tests
