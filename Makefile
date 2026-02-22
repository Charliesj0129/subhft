# HFT Platform Makefile
# Unified CLI for development, testing, and CI

.PHONY: dev test test-all coverage lint format typecheck benchmark start stop logs swarm-start swarm-stop swarm-logs build-rust clean help

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
# Help
# ============================================================================

help: ## Show this help message
	@echo "HFT Platform Development Commands"
	@echo ""
	@echo "Usage: make [target]"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
