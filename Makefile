.PHONY: help install test test-unit test-integration lint typecheck fmt check gate schema clean

VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/activate:
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip

install: $(VENV)/bin/activate ## Create the venv and install with dev extras
	$(PIP) install --quiet -e ".[dev]"

lint: ## ruff check + format check
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests

fmt: ## Apply ruff formatting and autofixes
	$(PY) -m ruff check --fix src tests
	$(PY) -m ruff format src tests

typecheck: ## mypy strict over the package
	$(PY) -m mypy

test-unit: ## Fast unit tests
	$(PY) -m pytest tests/unit -q

test-integration: ## Subprocess/crash tests
	$(PY) -m pytest tests/integration -q

test: ## Everything
	$(PY) -m pytest -q

check: lint typecheck test ## What CI runs

gate: check ## Phase 0 acceptance gate: 100 jobs, kill -9, exactly once
	@echo "--- Phase 0 gate: durability drill ---"
	$(PY) -m pytest tests/integration/test_crash_recovery.py -q
	@echo "Phase 0 gate PASSED"

schema: ## Regenerate db/schema.sql from db/migrations/
	$(PY) -m cindraleads.cli db schema-dump

clean: ## Remove caches and local state
	rm -rf .pytest_cache .mypy_cache .ruff_cache var
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
