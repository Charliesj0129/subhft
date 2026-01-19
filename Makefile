.PHONY: install dev test coverage lint format typecheck blackbox-tests regression-tests stress-tests system-tests acceptance-tests hooks clean run-sim run-prod backtest ops-up ops-down notebook

PYTHON := python3
ifneq ("$(wildcard .venv/bin/python)","")
PYTHON := .venv/bin/python
endif
CLI := $(PYTHON) -m hft_platform.cli

install:
	uv sync --no-dev

dev:
	uv sync --dev
	cp -n .env.example .env || true

test:
	uv run pytest tests/ --ignore=tests/stress --ignore=tests/benchmark --ignore=tests/integration/test_persistence.py

coverage:
	uv run pytest --cov=src/hft_platform --cov-report=term-missing tests/ --ignore=tests/stress --ignore=tests/benchmark --ignore=tests/integration/test_persistence.py

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy

blackbox-tests:
	PYTHONPATH=src uv run pytest -m blackbox

regression-tests:
	PYTHONPATH=src uv run pytest -m regression

stress-tests:
	PYTHONPATH=src HFT_RUN_STRESS=1 uv run pytest -m stress

system-tests:
	./scripts/run_system_tests.sh

acceptance-tests:
	PYTHONPATH=src uv run pytest -m acceptance

hooks:
	uv run pre-commit install

clean:
	rm -rf build dist *.egg-info
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -delete

# Run commands
run-sim:
	$(CLI) run --mode sim

run-prod:
	$(CLI) run --mode live

# Example: make backtest STRATEGY=simple_mm_demo DATE=2024-01-01
backtest:
	$(CLI) backtest run --strategy $(STRATEGY) --date $(DATE) --report

# Ops commands
ops-up:
	docker-compose -f ops/docker/docker-compose.yml up -d

ops-down:
	docker-compose -f ops/docker/docker-compose.yml down

# Research
notebook:
	jupyter lab --notebook-dir=notebooks
