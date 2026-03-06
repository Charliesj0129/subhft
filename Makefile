# HFT Platform Makefile
# Unified CLI for development, testing, and CI

.PHONY: dev test test-all coverage lint format typecheck benchmark start stop logs start-engine start-monitor start-maintenance swarm-start swarm-stop swarm-logs build-rust clean help recorder-status wal-dlq-status wal-dlq-replay wal-dlq-replay-dry-run wal-manifest-tmp-cleanup drill-ck-down drill-wal-pressure drill-loader-lag verify-ce3 wal-archive-cleanup soak-daily-report soak-weekly-report soak-canary-report deploy-drift-snapshot deploy-drift-check deploy-pre-sync-template release-channel-gate release-channel-promote release-converge release-converge-scan release-converge-clean reliability-monthly-pack roadmap-delivery-check ch-query-guard-check ch-query-guard-run ch-query-guard-suite env-vars-guard feature-canary-report callback-latency-report incident-timeline history-repair research research-optimize research-init research-converge-tools research-clean research-audit research-index research-run research-triage research-scaffold research-report research-fetch-paper research-search-papers research-paper-prototype research-record-paper research-summarize-paper research-check-paper-governance research-gen-synth-lob research-stamp-data-meta research-validate-data-meta

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

verify-ce3: ## Verify CE3 hardening (scale-out + replay contract + outage drills)
	uv run pytest --no-cov \
		tests/integration/test_wal_loader_scale_out.py \
		tests/spec/test_replay_safety_contract.py \
		tests/integration/test_wal_outage_drills.py -q

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

start-engine: ## Start HFT engine + core infra only — single runtime (no maintenance shell)
	docker compose up -d --build clickhouse redis hft-engine wal-loader

start-monitor: ## Start observability stack only
	docker compose up -d prometheus grafana alertmanager node-exporter

start-maintenance: ## Start maintenance shell (hft-base profile, no feed runtime)
	docker compose --profile maintenance up -d hft-base

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
	rm -rf htmlcov/ .coverage coverage.xml 2>/dev/null || true

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

wal-dlq-status: ## Show WAL DLQ status (count/bytes/age) and write artifacts
	python3 scripts/wal_dlq_ops.py status --wal-dir "$${WAL_DIR:-.wal}" --archive-dir "$${WAL_ARCHIVE_DIR:-.wal/archive}" --output-dir outputs/wal_dlq $(if $(filter 1,$(WAL_ALLOW_WARN)),--allow-warn-exit-zero,)

wal-dlq-replay: ## Replay WAL DLQ files into ClickHouse (live)
	python3 scripts/wal_dlq_ops.py replay --wal-dir "$${WAL_DIR:-.wal}" --archive-dir "$${WAL_ARCHIVE_DIR:-.wal/archive}" --ch-host "$${CH_HOST:-$${HFT_CLICKHOUSE_HOST:-clickhouse}}" --ch-port "$${CH_PORT:-$${HFT_CLICKHOUSE_PORT:-9000}}" --output-dir outputs/wal_dlq $(if $(MAX_FILES),--max-files "$(MAX_FILES)",)

wal-dlq-replay-dry-run: ## Dry-run WAL DLQ replay (no insert/move)
	python3 scripts/wal_dlq_ops.py replay --wal-dir "$${WAL_DIR:-.wal}" --archive-dir "$${WAL_ARCHIVE_DIR:-.wal/archive}" --ch-host "$${CH_HOST:-$${HFT_CLICKHOUSE_HOST:-clickhouse}}" --ch-port "$${CH_PORT:-$${HFT_CLICKHOUSE_PORT:-9000}}" --output-dir outputs/wal_dlq --dry-run --allow-warn-exit-zero $(if $(MAX_FILES),--max-files "$(MAX_FILES)",)

wal-manifest-tmp-cleanup: ## Cleanup orphan WAL *.tmp files (manifest temp leftovers)
	python3 scripts/wal_dlq_ops.py cleanup-tmp --wal-dir "$${WAL_DIR:-.wal}" --archive-dir "$${WAL_ARCHIVE_DIR:-.wal/archive}" --output-dir outputs/wal_dlq --min-age-seconds "$${MIN_AGE_SECONDS:-300}" $(if $(filter 1,$(DRY_RUN)),--dry-run,)

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

WAL_ARCHIVE_DIR ?= .wal/archive
WAL_KEEP_DAYS   ?= 7

wal-archive-cleanup: ## Delete WAL archive files older than WAL_KEEP_DAYS (default 7). Prompts for confirmation.
	@echo "=== WAL archive cleanup: keeping last $(WAL_KEEP_DAYS) days ==="
	@echo "Archive dir: $(WAL_ARCHIVE_DIR)"
	@echo ""
	@if [ ! -d "$(WAL_ARCHIVE_DIR)" ]; then \
		echo "Directory $(WAL_ARCHIVE_DIR) does not exist — nothing to clean."; \
		exit 0; \
	fi
	@echo "Files to be deleted:"
	@find $(WAL_ARCHIVE_DIR) -name "*.wal" -mtime +$(WAL_KEEP_DAYS) -print || true
	@echo ""
	@read -p "Confirm delete? [y/N] " confirm; \
	if [ "$$confirm" = "y" ] || [ "$$confirm" = "Y" ]; then \
		find $(WAL_ARCHIVE_DIR) -name "*.wal" -mtime +$(WAL_KEEP_DAYS) -delete; \
		echo "Done. Remaining files:"; \
		find $(WAL_ARCHIVE_DIR) -name "*.wal" | wc -l; \
	else \
		echo "Aborted — no files deleted."; \
	fi

soak-daily-report: ## Generate daily soak acceptance report from local deployment
	python3 scripts/soak_acceptance.py daily --project-root . --prom-url http://localhost:9091 --output-dir outputs/soak_reports --allow-warn-exit-zero

soak-weekly-report: ## Generate weekly soak summary from latest 7 daily reports
	python3 scripts/soak_acceptance.py weekly --project-root . --prom-url http://localhost:9091 --output-dir outputs/soak_reports

soak-canary-report: ## Evaluate feed canary thresholds from recent daily soak reports
	python3 scripts/soak_acceptance.py canary --project-root . --prom-url http://localhost:9091 --output-dir outputs/soak_reports --window-days 10 --min-trading-days 5 --min-first-quote-pass-ratio 1.0 --max-reconnect-failure-ratio 0.2 --max-watchdog-callback-reregister 120 --allow-warn-exit-zero

deploy-drift-snapshot: ## Create deployment drift baseline snapshot (outputs/deploy_guard/snapshots)
	python3 scripts/deploy_drift_guard.py snapshot --project-root . --output-dir outputs/deploy_guard --label baseline

deploy-drift-check: ## Compare current deployment state against BASELINE snapshot
	@if [ -z "$(BASELINE)" ]; then \
		echo "Usage: make deploy-drift-check BASELINE=outputs/deploy_guard/snapshots/<file>.json"; \
		exit 2; \
	fi
	python3 scripts/deploy_drift_guard.py check --project-root . --output-dir outputs/deploy_guard --baseline "$(BASELINE)"

deploy-pre-sync-template: ## Generate pre-sync artifact bundle (snapshot + backup + rollback template)
	@if [ -z "$(CHANGE_ID)" ]; then \
		echo "Usage: make deploy-pre-sync-template CHANGE_ID=CHG-YYYYMMDD-XX"; \
		exit 2; \
	fi
	python3 scripts/deploy_drift_guard.py prepare --project-root . --output-dir outputs/deploy_guard --change-id "$(CHANGE_ID)"

release-channel-gate: ## Evaluate canary->stable release gate using latest canary/drift/pre-sync artifacts
	@if [ -z "$(CHANGE_ID)" ]; then \
		echo "Usage: make release-channel-gate CHANGE_ID=CHG-YYYYMMDD-XX"; \
		exit 2; \
	fi
	python3 scripts/release_channel_guard.py gate --project-root . --output-dir outputs/deploy_guard --soak-dir outputs/soak_reports --change-id "$(CHANGE_ID)" --min-trading-days 5 --max-report-age-hours 72

release-channel-promote: ## Apply stable promotion record when release gate passes
	@if [ -z "$(CHANGE_ID)" ]; then \
		echo "Usage: make release-channel-promote CHANGE_ID=CHG-YYYYMMDD-XX [ACTOR=ops]"; \
		exit 2; \
	fi
	python3 scripts/release_channel_guard.py promote --project-root . --output-dir outputs/deploy_guard --soak-dir outputs/soak_reports --change-id "$(CHANGE_ID)" --min-trading-days 5 --max-report-age-hours 72 --actor "$${ACTOR:-ops}" --apply

release-converge-scan: ## Generate deep inventory snapshot only (ls/tree/git/du) for release convergence
	python3 scripts/release_converge.py --project-root . --output-dir outputs/release_converge --tree-depth "$${TREE_DEPTH:-3}" --skip-clean --skip-gate

release-converge-clean: ## Deep clean caches/artifacts then run release gates (roadmap + targeted tests/lint)
	python3 scripts/release_converge.py --project-root . --output-dir outputs/release_converge --tree-depth "$${TREE_DEPTH:-3}" $(if $(filter 1,$(CLEAN_RUST)),--clean-rust,)

release-converge: release-converge-clean ## Alias: converge repository to release-ready state

reliability-monthly-pack: ## Generate monthly reliability review pack (soak/backlog/drift/disk/drill/query-guard/feature-canary/callback-latency)
	python3 scripts/reliability_review_pack.py --project-root . --soak-dir outputs/soak_reports --deploy-dir outputs/deploy_guard --query-guard-dir outputs/query_guard --feature-canary-dir outputs/feature_canary --callback-latency-dir outputs/callback_latency --output-dir outputs/reliability/monthly --month "$${MONTH:-$(shell date +%Y-%m)}" --disk-path . --disk-path .wal --min-query-guard-runs "$${QUERY_GUARD_MIN_RUNS:-1}" --min-query-guard-suite-runs "$${QUERY_GUARD_MIN_SUITE_RUNS:-1}" --min-feature-canary-runs "$${FEATURE_CANARY_MIN_RUNS:-1}" --min-callback-latency-runs "$${CALLBACK_LATENCY_MIN_RUNS:-1}" $(if $(filter 1,$(RUN_DRILL)),--run-drill-suite,) --allow-warn-exit-zero

roadmap-delivery-check: ## Validate TODO/ROADMAP governance (skills/RACI/agent roles/KPI) and emit execution board
	python3 scripts/roadmap_delivery_executor.py --project-root . --todo docs/TODO.md --roadmap ROADMAP.md --benchmark benchmark.json --paper-index research/knowledge/paper_index.json --output-dir outputs/roadmap_execution --allow-warn-exit-zero
	python3 scripts/roadmap_delivery_guard.py --todo docs/TODO.md --roadmap ROADMAP.md --execution-dir outputs/roadmap_execution --max-artifact-age-hours "$${MAX_ARTIFACT_AGE_HOURS:-168}" --output-dir outputs/roadmap_delivery $(if $(filter 1,$(ALLOW_WARN)),--allow-warn-exit-zero,)

roadmap-delivery-execute: ## Materialize WS-G/WS-H deliverables (hotpath matrix, cutover backlog json/md, source catalog, quality, readiness)
	python3 scripts/roadmap_delivery_executor.py --project-root . --todo docs/TODO.md --roadmap ROADMAP.md --benchmark benchmark.json --pyspy-triage outputs/research_maintenance/pyspy_triage.json --perf-snapshot outputs/perf_gate_latency_snapshot.clean.json --stage-probe outputs/latency_stage_probe_custom_nonorder.json --paper-index research/knowledge/paper_index.json --runs-root research/experiments/runs --promotions-root research/experiments/promotions --output-dir outputs/roadmap_execution $(if $(filter 1,$(ALLOW_WARN)),--allow-warn-exit-zero,)

ch-query-guard-check: ## Guard-check ClickHouse SQL (read-only + full-scan policy)
	@if [ -z "$(QUERY)" ] && [ -z "$(QUERY_FILE)" ]; then \
		echo "Usage: make ch-query-guard-check QUERY='SELECT ...' [ALLOW_FULL_SCAN=1]"; \
		echo "   or: make ch-query-guard-check QUERY_FILE=path/to/query.sql [ALLOW_FULL_SCAN=1]"; \
		exit 2; \
	fi
	python3 scripts/ch_query_guard.py check \
		$(if $(QUERY),--query "$(QUERY)",--query-file "$(QUERY_FILE)") \
		--output-dir outputs/query_guard \
		$(if $(filter 1,$(ALLOW_FULL_SCAN)),--allow-full-scan,) \
		--allow-warn-exit-zero

ch-query-guard-run: ## Execute guarded ClickHouse SQL with memory/time/result limits
	@if [ -z "$(QUERY)" ] && [ -z "$(QUERY_FILE)" ]; then \
		echo "Usage: make ch-query-guard-run QUERY='SELECT ... LIMIT 100' [ALLOW_WARN_EXEC=1]"; \
		echo "   or: make ch-query-guard-run QUERY_FILE=path/to/query.sql [ALLOW_WARN_EXEC=1]"; \
		exit 2; \
	fi
	python3 scripts/ch_query_guard.py run \
		$(if $(QUERY),--query "$(QUERY)",--query-file "$(QUERY_FILE)") \
		--output-dir outputs/query_guard \
		$(if $(filter 1,$(ALLOW_FULL_SCAN)),--allow-full-scan,) \
		$(if $(filter 1,$(ALLOW_WARN_EXEC)),--allow-warn-execute,)

ch-query-guard-suite: ## Run baseline guarded ClickHouse query suite for periodic evidence generation
	python3 scripts/ch_query_guard_suite.py \
		--profile config/monitoring/query_guard_suite_baseline.json \
		--output-dir outputs/query_guard \
		--container "$${CH_CONTAINER:-clickhouse}" \
		--host "$${CH_HOST:-localhost}" \
		--port "$${CH_PORT:-9000}" \
		--user "$${CH_USER:-default}" \
		--timeout-s "$${CH_QUERY_TIMEOUT_S:-60}"

env-vars-guard: ## Verify runbook HFT_* vars are documented in env-vars reference
	python3 scripts/env_var_reference_guard.py --project-root . --output-dir outputs/env_var_guard

feature-canary-report: ## Evaluate feature shadow/canary guardrails from Prometheus and emit report
	python3 scripts/feature_canary_guard.py --prom-url "$${PROM_URL:-http://localhost:9091}" --window "$${WINDOW:-1h}" --output-dir outputs/feature_canary $(if $(filter 1,$(ALLOW_WARN)),--allow-warn-exit-zero,)

callback-latency-report: ## Evaluate Shioaji callback ingress latency/queue/parser guardrails from Prometheus
	python3 scripts/callback_latency_guard.py --prom-url "$${PROM_URL:-http://localhost:9091}" --window "$${WINDOW:-30m}" --output-dir outputs/callback_latency $(if $(filter 1,$(ALLOW_WARN)),--allow-warn-exit-zero,)

incident-timeline: ## Render incident timeline artifact from decision trace JSONL
	@if [ -z "$(TRACE_FILE)" ]; then \
		echo "Usage: make incident-timeline TRACE_FILE=outputs/decision_traces/<day>.jsonl [TRACE_ID=topic:seq] [FORMAT=md|json] [OUT=path]"; \
		exit 2; \
	fi
	PYTHONPATH=src python3 scripts/render_incident_timeline.py "$(TRACE_FILE)" $(if $(TRACE_ID),--trace-id "$(TRACE_ID)",) --format "$${FORMAT:-md}" --out "$${OUT:-outputs/incidents/timeline.$${FORMAT:-md}}"

history-repair: ## Repair fragmented historical parquet exports and resample to complete OHLCV
	@if [ -z "$(INPUTS)" ] || [ -z "$(OUT)" ]; then \
		echo "Usage: make history-repair INPUTS='data/a.parquet data/b.parquet.part' OUT=outputs/history/repaired.parquet [ARGS='--target-ms 1000 --report-out outputs/history/repaired_report.json']"; \
		exit 2; \
	fi
	uv run python scripts/repair_history_resample.py $(foreach f,$(INPUTS),--input $(f)) --out "$(OUT)" $(ARGS)

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

research-check-paper-governance: ## Check Gate-E paper-trade governance readiness
	@if [ -z "$(ALPHA)" ]; then \
		echo "Usage: make research-check-paper-governance ALPHA=<alpha_id> [ARGS='--strict --out outputs/paper_governance.json']"; \
		exit 2; \
	fi
	uv run python -m research check-paper-governance --alpha-id "$(ALPHA)" $(ARGS)

research-gen-synth-lob: ## Generate synthetic LOB dataset + metadata sidecar
	@if [ -z "$(OUT)" ]; then \
		echo "Usage: make research-gen-synth-lob OUT=research/data/processed/<name>.npy [ARGS='--version v2 --rng-seed 42 --symbols TXF,MXF --split train']"; \
		exit 2; \
	fi
	uv run python research/tools/synth_lob_gen.py --out "$(OUT)" $(ARGS)

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
