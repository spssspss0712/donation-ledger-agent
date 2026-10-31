PY ?= python
export DATABASE_URL ?= postgresql+psycopg://ledger:ledger@localhost:5432/ledger
export TEST_DATABASE_URL ?= postgresql+psycopg://ledger:ledger@localhost:5432/ledger_test
export DEMO_DATABASE_URL ?= postgresql+psycopg://ledger:ledger@localhost:5432/ledger_demo

.PHONY: install db test lint typecheck audit demo migrate check

install:
	$(PY) -m pip install -r requirements-dev.txt

db:  ## start a local PostgreSQL with the three databases used here
	docker compose up -d db

migrate:
	alembic upgrade head

test:
	$(PY) -m pytest

lint:
	ruff check . && ruff format --check .

typecheck:
	mypy

audit:
	pip-audit -r requirements.txt

demo:
	$(PY) -m donation_ledger.demo

check: lint typecheck test
