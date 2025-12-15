.PHONY: install test lint format clean run-sim run-prod backtest ops-up ops-down notebook

PYTHON := python3
CLI := $(PYTHON) -m hft_platform.cli

install:
	pip install -e .

test:
	pytest tests/unit

lint:
	ruff check .

format:
	ruff check --fix .

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
