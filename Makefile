# HFT Platform Makefile
# Unified CLI for development, testing, and CI

.PHONY: dev test test-all coverage lint format typecheck benchmark start stop logs swarm-start swarm-stop swarm-logs build-rust clean help recorder-status drill-ck-down drill-wal-pressure drill-loader-lag research research-optimize research-init research-converge-tools research-clean research-audit research-index research-run research-triage research-scaffold research-report research-fetch-paper research-search-papers research-paper-prototype research-record-paper research-summarize-paper research-stamp-data-meta research-validate-data-meta

# Default target
.DEFAULT_GOAL := help

# ============================================================================
# Development
# ============================================================================

dev: ## Install dev dependencies
	uv sync --dev

build-rust: ## Build Rust extension with maturin
	uv run maturin develop --manifest-path rust_core/Cargo.toml

# ============================================================================
# Testing
# ============================================================================

test: ## Run unit tests
	uv run pytest tests/unit -v --tb=short

test-all: ## Run all tests (unit + integration)
	uv run pytest tests/ -v --tb=short

test-integration: ## Run integration tests only
	uv run pytest tests/integration -v --tb=short -m "not slow"

coverage: ## Run tests with coverage (70% minimum)
	uv run pytest tests/unit \
		--cov=src/hft_platform \
		--cov-branch \
		--cov-report=term-missing \
		--cov-fail-under=70

coverage-html: ## Generate HTML coverage report
	uv run pytest tests/unit \
		--cov=src/hft_platform \
		--cov-branch \
		--cov-report=html:htmlcov
	@echo "Coverage report: htmlcov/index.html"

# ============================================================================
# Code Quality
# ============================================================================

lint: ## Run ruff linter
	uv run ruff check src/ tests/

lint-fix: ## Run ruff linter with auto-fix
	uv run ruff check --fix src/ tests/

format: ## Format code with ruff
	uv run ruff format src/ tests/

format-check: ## Check code formatting without changes
	uv run ruff format --check src/ tests/

typecheck: ## Run mypy type checker
	uv run mypy

check: lint typecheck ## Run all code quality checks

# ============================================================================
# Benchmarks
# ============================================================================

benchmark: ## Run benchmarks (pytest-benchmark)
	uv run pytest tests/benchmark \
		--benchmark-only \
		--benchmark-json=benchmark.json \
		-v

benchmark-baseline: ## Generate benchmark baseline for Darwin Gate
	uv run pytest tests/benchmark \
		--benchmark-only \
		--benchmark-json=tests/benchmark/.benchmark_baseline.json \
		--benchmark-min-rounds=10 \
		-v
	@echo "Baseline saved to tests/benchmark/.benchmark_baseline.json"

benchmark-compare: ## Compare current benchmarks against baseline
	uv run pytest tests/benchmark \
		--benchmark-only \
		--benchmark-json=benchmark.json \
		-v
	uv run python scripts/benchmark_gate.py \
		--baseline tests/benchmark/.benchmark_baseline.json \
		--current benchmark.json \
		--threshold 0.10

# ============================================================================
# Docker / Services
# ============================================================================

start: ## Start services with Docker Compose (default)
	docker compose up -d --build

stop: ## Stop services with Docker Compose
	docker compose down

logs: ## Show hft-engine logs (Docker Compose)
	docker compose logs -f hft-engine

swarm-start: ## Build image and deploy Docker Swarm stack (optional)
	docker swarm init >/dev/null 2>&1 || true
	docker build -t $${HFT_IMAGE:-hft-platform:latest} .
	docker stack deploy -c docker-stack.yml hft

swarm-stop: ## Remove Docker Swarm stack
	docker stack rm hft

swarm-logs: ## Show hft-engine service logs (Swarm)
	docker service logs -f hft_hft-engine

# ============================================================================
# Cleanup
# ============================================================================

clean: ## Clean build artifacts and caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf htmlcov/ .coverage coverage.xml benchmark.json 2>/dev/null || true

clean-rust: ## Clean Rust build artifacts
	cargo clean --manifest-path rust_core/Cargo.toml

clean-all: clean clean-rust ## Clean everything

# ============================================================================
# CI Simulation
# ============================================================================

ci: format-check lint typecheck coverage ## Run full CI pipeline locally

# ============================================================================
# Failure Simulation / Drill Targets
# ============================================================================

recorder-status: ## Show recorder WAL backlog and ClickHouse status
	uv run hft recorder status

drill-ck-down: ## Drill: stop ClickHouse for 30s then restart (tests WAL fallback)
	@echo "Stopping ClickHouse for 30s to test WAL fallback..."
	docker compose stop clickhouse
	sleep 30
	docker compose start clickhouse
	@echo "ClickHouse restarted. Check WAL drain with: make recorder-status"

drill-wal-pressure: ## Drill: simulate disk pressure circuit breaker
	@echo "Simulating disk pressure (HFT_WAL_DISK_MIN_MB=999999)..."
	HFT_WAL_DISK_MIN_MB=999999 uv run hft recorder status

drill-loader-lag: ## Drill: show WAL backlog info and instructions for lag simulation
	@echo "WAL Loader Lag Drill:"
	@echo "  1. Stop ClickHouse: docker compose stop clickhouse"
	@echo "  2. Generate load:   uv run hft run sim (let it produce WAL files)"
	@echo "  3. Check backlog:   make recorder-status"
	@echo "  4. Restore:         docker compose start clickhouse"
	@echo ""
	uv run hft recorder status

# ============================================================================
# Research Factory
# ============================================================================

research-init: ## Initialize canonical research layout
	uv run python -m research.factory init

research-converge-tools: ## Move non-core scripts to research/tools/legacy
	uv run python -m research.factory converge-tools

research-clean: ## Remove research cache artifacts (__pycache__, .pyc, numba caches)
	uv run python -m research.factory clean

research-audit: ## Audit research pipeline contract and write report
	uv run python -m research.factory audit

research-index: ## Build machine-readable research pipeline index
	uv run python -m research.factory index

research-optimize: ## One-flow factory optimize (init -> converge-tools -> clean -> audit -> index)
	uv run python -m research.factory optimize

research: ## Official single entrance: strict pipeline with factory optimize preflight
	@if [ -z "$(ALPHA)" ] || [ -z "$(OWNER)" ] || [ -z "$(DATA)" ]; then \
		echo "Usage: make research ALPHA=<alpha_id> OWNER=<owner> DATA='<path1.npy [path2.npy ...]' [ARGS='--min-sharpe-oos-gate-d 1.2']"; \
		exit 2; \
	fi
	uv run python -m research.pipeline run --alpha-id "$(ALPHA)" --owner "$(OWNER)" --data $(DATA) $(ARGS)

research-run: research ## Backward-compatible alias of `make research`

research-triage: ## Internal debug flow (requires HFT_RESEARCH_ALLOW_TRIAGE=1)
	@if [ "$$HFT_RESEARCH_ALLOW_TRIAGE" != "1" ]; then \
		echo "research-triage is disabled by default. Export HFT_RESEARCH_ALLOW_TRIAGE=1 to continue."; \
		exit 2; \
	fi
	@if [ -z "$(ALPHA)" ] || [ -z "$(OWNER)" ] || [ -z "$(DATA)" ]; then \
		echo "Usage: make research-triage ALPHA=<alpha_id> OWNER=<owner> DATA='<path1.npy [path2.npy ...]' [ARGS='--skip-gate-b-tests --no-promote --allow-gate-fail']"; \
		exit 2; \
	fi
	uv run python -m research.pipeline triage --alpha-id "$(ALPHA)" --owner "$(OWNER)" --data $(DATA) $(ARGS)

research-scaffold: ## Scaffold a new governed alpha package under research/alphas/
	@if [ -z "$(ALPHA)" ]; then \
		echo "Usage: make research-scaffold ALPHA=<alpha_id> [ARGS='--paper ref --complexity O1']"; \
		exit 2; \
	fi
	uv run python -m research scaffold $(ALPHA) $(ARGS)

research-report: ## Render promotion report markdown for a given alpha
	@if [ -z "$(ALPHA)" ]; then \
		echo "Usage: make research-report ALPHA=<alpha_id> [ARGS='--out report.md']"; \
		exit 2; \
	fi
	uv run python research/tools/render_promotion_report.py --alpha-id "$(ALPHA)" $(ARGS)

research-fetch-paper: ## Fetch and index an arxiv paper (e.g. make research-fetch-paper ARXIV=2408.03594)
	@if [ -z "$(ARXIV)" ]; then \
		echo "Usage: make research-fetch-paper ARXIV=<arxiv_id>"; \
		exit 2; \
	fi
	uv run python -m research fetch-paper "$(ARXIV)" $(ARGS)

research-search-papers: ## Search arxiv papers (e.g. make research-search-papers QUERY=\"order flow imbalance\")
	@if [ -z "$(QUERY)" ]; then \
		echo "Usage: make research-search-papers QUERY=\"<search terms>\""; \
		exit 2; \
	fi
	uv run python -m research search-papers "$(QUERY)" $(ARGS)

research-paper-prototype: ## Scaffold prototype directly from paper ref in paper_index
	@if [ -z "$(PAPER_REF)" ]; then \
		echo "Usage: make research-paper-prototype PAPER_REF=<ref|arxiv_id> [ARGS='--alpha-id my_alpha --complexity O1']"; \
		exit 2; \
	fi
	uv run python -m research paper-to-prototype "$(PAPER_REF)" $(ARGS)

research-record-paper: ## Record one paper-trade session
	@if [ -z "$(ALPHA)" ]; then \
		echo "Usage: make research-record-paper ALPHA=<alpha_id> [ARGS='--trading-day 2026-02-28 --fills 20 --pnl-bps 4.2']"; \
		exit 2; \
	fi
	uv run python -m research record-paper --alpha-id "$(ALPHA)" $(ARGS)

research-summarize-paper: ## Summarize paper-trade sessions for one alpha
	@if [ -z "$(ALPHA)" ]; then \
		echo "Usage: make research-summarize-paper ALPHA=<alpha_id> [ARGS='--out outputs/paper_summary.json']"; \
		exit 2; \
	fi
	uv run python -m research summarize-paper --alpha-id "$(ALPHA)" $(ARGS)

research-stamp-data-meta: ## Create data metadata sidecar for dataset
	@if [ -z "$(DATA_PATH)" ]; then \
		echo "Usage: make research-stamp-data-meta DATA_PATH=<path.npy|path.npz> [ARGS='--source-type real --owner charlie --symbols 2330']"; \
		exit 2; \
	fi
	uv run python -m research stamp-data-meta "$(DATA_PATH)" $(ARGS)

research-validate-data-meta: ## Validate data metadata sidecar for dataset
	@if [ -z "$(DATA_PATH)" ]; then \
		echo "Usage: make research-validate-data-meta DATA_PATH=<path.npy|path.npz> [ARGS='--meta <meta.json>']"; \
		exit 2; \
	fi
	uv run python -m research validate-data-meta "$(DATA_PATH)" $(ARGS)

# ============================================================================
# Help
# ============================================================================

help: ## Show this help message
	@echo "HFT Platform Development Commands"
	@echo ""
	@echo "Usage: make [target]"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
