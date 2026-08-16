.PHONY: help install test test-unit test-integration lint typecheck fmt check gate schema bench fixtures clean

VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/activate:
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip

install: $(VENV)/bin/activate ## Create the venv and install with dev + pipeline extras
	# extract/dedupe are Phase 3 runtime deps, not optional in practice: without them
	# textextract falls back to the stdlib parser and the dedupe ladder to difflib.
	# Both work, both are worse. osint carries dnspython: without it every DNS field
	# reads as unknown and T8_HYGIENE_GAP can never fire.
	# Install them so the Pi runs the measured path.
	$(PIP) install --quiet -e ".[dev,extract,dedupe,osint]"

lint: ## ruff check + format check
	$(PY) -m ruff check src tests scripts
	$(PY) -m ruff format --check src tests scripts

fmt: ## Apply ruff formatting and autofixes
	$(PY) -m ruff check --fix src tests scripts
	$(PY) -m ruff format src tests scripts

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

fixtures: ## Gather the Phase 1 HTML corpus (needs open outbound HTTPS - run on the Pi)
	$(PY) scripts/fetch_fixtures.py

bench: ## Phase 1 benchmark -> docs/BENCHMARKS.md (RUN ON THE PI, needs Ollama)
	$(PY) scripts/benchmark_models.py

clean: ## Remove caches and local state
	rm -rf .pytest_cache .mypy_cache .ruff_cache var
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
