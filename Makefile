# HFT Platform Makefile
# Unified CLI for development, testing, and CI

-include .env
export

.PHONY: dev build-rust test test-all test-integration verify-ce3 coverage coverage-html arch-gate dependency-boundary test-assertion-check test-name-check test-quality-pattern-check test-collection-check test-hygiene-check test-file test-node lint lint-fix format format-check typecheck check latency-gate-ci benchmark benchmark-baseline benchmark-compare start start-engine start-monitor start-maintenance stop logs swarm-start swarm-stop swarm-logs clean clean-rust clean-all ci recorder-status wal-dlq-status wal-dlq-replay wal-dlq-replay-dry-run wal-manifest-tmp-cleanup drill-ck-down drill-wal-pressure drill-loader-lag wal-archive-cleanup soak-daily-report soak-weekly-report soak-canary-report deploy-drift-snapshot deploy-drift-check deploy-pre-sync-template release-channel-gate release-channel-promote release-converge-scan release-converge-clean release-converge release-converge-mvp release-first-ops-gate release-first-ops-promote release-readiness-check canary-snapshot canary-evaluate canary-auto reliability-monthly-pack roadmap-delivery-check roadmap-delivery-execute ch-query-guard-check ch-query-guard-run ch-query-guard-suite env-vars-guard feature-canary-report callback-latency-report incident-timeline history-repair research-init research-converge-tools research-clean research-audit research-audit-strict research-index research-optimize research research-run research-triage research-scaffold research-report research-fetch-paper research-search-papers research-paper-prototype research-record-paper research-summarize-paper research-check-paper-governance research-gen-synth-lob research-stamp-data-meta research-validate-data-meta monitor-remote experiment-gc experiment-gc-dry-run help pre-market-check post-market-check alert-test drill-recon-mismatch rollback-drill git-precheck git-postcheck git-session-check

PY ?= uv run python

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
	uv run pytest tests/integration -v --tb=short -m "not slow" --no-cov

verify-ce3: ## Verify CE3 hardening (scale-out + replay contract + outage drills)
	uv run pytest --no-cov \
		tests/integration/test_wal_loader_scale_out.py \
		tests/spec/test_replay_safety_contract.py \
		tests/integration/test_wal_outage_drills.py -q

test-collect: ## Verify all tests can be collected (no import errors)
	uv run pytest tests/unit --collect-only -q

coverage: ## Run tests with coverage (70% minimum)
	uv run pytest tests/unit --cov-fail-under=70

coverage-html: ## Generate HTML coverage report
	uv run pytest tests/unit --cov-report=html:htmlcov
	@echo "Coverage report: htmlcov/index.html"

# ============================================================================
# Code Quality
# ============================================================================

arch-gate: ## Run architecture conformance gate
	$(PY) scripts/arch_conformance_gate.py

dependency-boundary: ## Enforce import-layer and protected-module contracts
	env PYTHONPATH=src uv tool run --from import-linter lint-imports --config $(CURDIR)/.importlinter

test-assertion-check: ## Check test functions have assertions
	$(PY) scripts/check_test_assertions.py --max-advisory 10

test-name-check: ## Enforce behavior-oriented test naming
	$(PY) scripts/check_test_naming.py

test-quality-pattern-check: ## Detect weak tautological test patterns
	$(PY) scripts/check_test_quality_patterns.py

test-hygiene-check: test-assertion-check test-name-check test-quality-pattern-check ## Run test quality gates

test-file: ## Run one test file without repository-wide coverage gate
	@test -n "$(FILE)" || (echo "Usage: make test-file FILE=tests/unit/test_x.py" && exit 1)
	uv run pytest --no-cov $(FILE) -q

test-node: ## Run one pytest node without repository-wide coverage gate
	@test -n "$(NODE)" || (echo "Usage: make test-node NODE=tests/unit/test_x.py::test_y" && exit 1)
	uv run pytest --no-cov $(NODE) -q

test-collection-check: ## Verify zero test collection errors
	$(PY) scripts/check_test_collection.py

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

discipline: ## Run AST-based discipline enforcement (9 rules)
	uv run python scripts/check_discipline.py --ci

discipline-strict: ## Run discipline enforcement in strict mode (warnings block too)
	uv run python scripts/check_discipline.py --ci --strict

git-precheck: ## Run git precondition checks (AWG-01/03) before merge/rebase
	bash scripts/check_git_preconditions.sh --pre-merge

git-postcheck: ## Verify git state after merge/rebase (no conflict markers)
	bash scripts/check_git_preconditions.sh --post-merge

git-session-check: ## Full git hygiene check (worktrees, branches, stash, conflicts)
	bash scripts/check_git_preconditions.sh --full

check: lint typecheck discipline dependency-boundary test-hygiene-check ## Run all code quality checks

# ============================================================================
# Benchmarks
# ============================================================================

latency-gate-ci: ## Run latency regression benchmarks (CI)
	uv run pytest tests/bench/ -m bench -v --tb=short

benchmark: ## Run benchmarks (pytest-benchmark)
	uv run pytest tests/benchmark \
		--no-cov \
		--benchmark-only \
		--benchmark-json=benchmark.json \
		-v

benchmark-baseline: ## Generate benchmark baseline for Darwin Gate
	uv run pytest tests/benchmark \
		--no-cov \
		--benchmark-only \
		--benchmark-json=tests/benchmark/.benchmark_baseline.json \
		--benchmark-min-rounds=10 \
		-v
	@echo "Baseline saved to tests/benchmark/.benchmark_baseline.json"

benchmark-compare: ## Compare current benchmarks against baseline
	uv run pytest tests/benchmark \
		--no-cov \
		--benchmark-only \
		--benchmark-json=benchmark.json \
		-v
	$(PY) scripts/benchmark_gate.py \
		--baseline tests/benchmark/.benchmark_baseline.json \
		--current benchmark.json \
		--threshold 0.10

# ============================================================================
# Docker / Services
# ============================================================================

start: ## Start services with Docker Compose (default)
	# P2-d (2026-04-27): export GIT_SHA + BUILD_TS for docker-compose build args
	HFT_GIT_SHA=$$(git rev-parse HEAD 2>/dev/null || echo unknown) \
		HFT_BUILD_TS=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
		docker compose up -d --build

start-engine: ## Start HFT engine + core infra only — single runtime (no maintenance shell)
	HFT_GIT_SHA=$$(git rev-parse HEAD 2>/dev/null || echo unknown) \
		HFT_BUILD_TS=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
		docker compose up -d --build clickhouse redis hft-engine wal-loader

start-monitor: ## Start observability stack only
	docker compose up -d prometheus grafana alertmanager node-exporter

monitor-remote: ## Start Signal Monitor TUI via SSH tunnel to remote ClickHouse
	bash scripts/run_signal_monitor.sh

start-maintenance: ## Start maintenance shell (hft-base profile, no feed runtime)
	docker compose --profile maintenance up -d hft-base

stop: ## Stop services with Docker Compose
	docker compose down

logs: ## Show hft-engine logs (Docker Compose)
	docker compose logs -f hft-engine

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

ci: format-check lint typecheck dependency-boundary test-hygiene-check coverage ## Run full CI pipeline locally

.PHONY: test-unit-ci coverage-branch-gate coverage-domain coverage-markdown test-integration-ci test-clickhouse-writer-smoke
.PHONY: perf-gate-default perf-gate-recorder-io perf-gate-risk-heavy perf-gate-feature-rust
.PHONY: benchmark-ci benchmark-darwin-gate drill-gateway-wal-hardening
.PHONY: research-feature-benchmark-matrix render-research-promotion-report security-audit
.PHONY: render-incident-timeline-json render-incident-timeline-md

test-unit-ci: ## Run unit tests in CI mode and emit coverage.xml
	uv run pytest tests/unit -q --cov=src/hft_platform --cov-report=term-missing --cov-report=xml --timeout=10 -p no:hypothesis -p no:faulthandler

coverage-branch-gate: ## Enforce minimum coverage threshold from latest unit-test run
	uv run coverage report --fail-under=70

coverage-domain: ## Enforce per-package coverage floors (risk/order/execution/gateway/recorder/alpha)
	uv run python scripts/check_coverage_domains.py coverage.xml

coverage-markdown: ## Print coverage summary in markdown-friendly text
	uv run coverage report

test-integration-ci: ## Run integration tests in CI mode (excluding slow markers)
	uv run pytest tests/integration -v --tb=short -m "not slow" --no-cov

test-clickhouse-writer-smoke: ## Smoke test ClickHouse writer roundtrip path
	uv run pytest tests/system/test_clickhouse_writer.py::test_clickhouse_writer_roundtrip --no-cov -q

perf-gate-default: ## Lightweight perf regression gate for default CI profile
	uv run python tests/benchmark/perf_regression_gate.py --runs 1 --json risk_perf_gate.json

perf-gate-recorder-io: ## Nightly perf gate: recorder I/O heavy drills
	uv run python tests/benchmark/perf_regression_gate.py --runs 1 --include-recorder-io --json recorder_perf_gate.json

perf-gate-risk-heavy: ## Nightly perf gate: risk/gateway heavy drills
	uv run python tests/benchmark/perf_regression_gate.py --runs 1 --include-risk-heavy --json risk_perf_gate.json

perf-gate-feature-rust: ## Nightly perf gate: feature engine rust drills
	uv run python tests/benchmark/perf_regression_gate.py --runs 1 --include-feature-rust --json feature_perf_gate.json

benchmark-ci: ## Run benchmark suite and export benchmark.json for Darwin gate
	uv run pytest tests/benchmark --no-cov --benchmark-only --benchmark-json=benchmark.json -v

benchmark-darwin-gate: ## Check benchmark regressions against baseline
	$(PY) scripts/benchmark_gate.py --baseline tests/benchmark/.benchmark_baseline.json --current benchmark.json --threshold "$${DARWIN_GATE_THRESHOLD:-0.10}"

drill-gateway-wal-hardening: verify-ce3 ## Gateway/WAL hardening integration drill bundle

research-feature-benchmark-matrix: ## Research wrapper benchmark matrix artifact
	$(PY) research/tools/feature_benchmark_matrix.py --runs "$${RUNS:-1}" --out research_feature_benchmark_matrix.json

render-research-promotion-report: ## Render promotion markdown from synthetic/nightly JSON artifact
	$(PY) research/tools/render_promotion_report.py research_feature_promotion_smoke.json --out research_feature_promotion_smoke.md

render-incident-timeline-json: ## Render incident timeline JSON from nightly_trace.jsonl
	PYTHONPATH=src $(PY) scripts/render_incident_timeline.py nightly_trace.jsonl --format json --out diagnostics_timeline.json

render-incident-timeline-md: ## Render incident timeline markdown from nightly_trace.jsonl
	PYTHONPATH=src $(PY) scripts/render_incident_timeline.py nightly_trace.jsonl --format md --out diagnostics_timeline.md

security-audit: ## Dependency security scan with pip-audit fallback to pip check
	@set -e; \
	if uv run python -c "import pip_audit" >/dev/null 2>&1; then \
		uv run python -m pip_audit --progress-spinner off | tee audit-output.txt; \
	else \
		echo "pip-audit unavailable; fallback to pip check" | tee audit-output.txt; \
		uv run python -m pip check | tee -a audit-output.txt; \
	fi

# ============================================================================
# Failure Simulation / Drill Targets
# ============================================================================

pre-market-check: ## Run pre-market health checks (Docker, ClickHouse, Redis, WAL, metrics)
	$(PY) scripts/pre_market_check.py

post-market-check: ## Run post-market health checks (WAL, recorder, ClickHouse records, PnL)
	$(PY) scripts/post_market_check.py

alert-test: ## Send test alert to alertmanager and verify delivery
	$(PY) scripts/alert_test.py

drill-recon-mismatch: ## Run reconciliation mismatch drill (integration test)
	uv run pytest tests/integration/test_recon_mismatch_drill.py -v --no-cov

recorder-status: ## Show recorder WAL backlog and ClickHouse status
	uv run hft recorder status

wal-dlq-status: ## Show WAL DLQ status (count/bytes/age) and write artifacts
	$(PY) scripts/wal_dlq_ops.py status --wal-dir "$${WAL_DIR:-.wal}" --archive-dir "$${WAL_ARCHIVE_DIR:-.wal/archive}" --output-dir outputs/wal_dlq $(if $(filter 1,$(WAL_ALLOW_WARN)),--allow-warn-exit-zero,)

wal-dlq-replay: ## Replay WAL DLQ files into ClickHouse (live)
	$(PY) scripts/wal_dlq_ops.py replay --wal-dir "$${WAL_DIR:-.wal}" --archive-dir "$${WAL_ARCHIVE_DIR:-.wal/archive}" --ch-host "$${CH_HOST:-$${HFT_CLICKHOUSE_HOST:-clickhouse}}" --ch-port "$${CH_PORT:-$${HFT_CLICKHOUSE_PORT:-9000}}" --output-dir outputs/wal_dlq $(if $(MAX_FILES),--max-files "$(MAX_FILES)",)

wal-dlq-replay-dry-run: ## Dry-run WAL DLQ replay (no insert/move)
	$(PY) scripts/wal_dlq_ops.py replay --wal-dir "$${WAL_DIR:-.wal}" --archive-dir "$${WAL_ARCHIVE_DIR:-.wal/archive}" --ch-host "$${CH_HOST:-$${HFT_CLICKHOUSE_HOST:-clickhouse}}" --ch-port "$${CH_PORT:-$${HFT_CLICKHOUSE_PORT:-9000}}" --output-dir outputs/wal_dlq --dry-run --allow-warn-exit-zero $(if $(MAX_FILES),--max-files "$(MAX_FILES)",)

wal-manifest-tmp-cleanup: ## Cleanup orphan WAL *.tmp files (manifest temp leftovers)
	$(PY) scripts/wal_dlq_ops.py cleanup-tmp --wal-dir "$${WAL_DIR:-.wal}" --archive-dir "$${WAL_ARCHIVE_DIR:-.wal/archive}" --output-dir outputs/wal_dlq --min-age-seconds "$${MIN_AGE_SECONDS:-300}" $(if $(filter 1,$(DRY_RUN)),--dry-run,)

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

rollback-drill: ## Drill: simulate rollback procedure and verify health restoration
	@echo "=== Rollback Drill ==="
	$(PY) scripts/rollback_drill.py

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
	$(PY) scripts/soak_acceptance.py daily --project-root . --prom-url http://localhost:9091 --output-dir outputs/soak_reports --allow-warn-exit-zero

soak-weekly-report: ## Generate weekly soak summary from latest 7 daily reports
	$(PY) scripts/soak_acceptance.py weekly --project-root . --prom-url http://localhost:9091 --output-dir outputs/soak_reports

soak-canary-report: ## Evaluate feed canary thresholds from recent daily soak reports
	$(PY) scripts/soak_acceptance.py canary --project-root . --prom-url http://localhost:9091 --output-dir outputs/soak_reports --window-days 10 --min-trading-days 5 --min-first-quote-pass-ratio 1.0 --max-reconnect-failure-ratio 0.2 --max-watchdog-callback-reregister 120 --allow-warn-exit-zero

deploy-drift-snapshot: ## Create deployment drift baseline snapshot (outputs/deploy_guard/snapshots)
	$(PY) scripts/deploy_drift_guard.py snapshot --project-root . --output-dir outputs/deploy_guard --label baseline

deploy-drift-check: ## Compare current deployment state against BASELINE snapshot
	@if [ -z "$(BASELINE)" ]; then \
		echo "Usage: make deploy-drift-check BASELINE=outputs/deploy_guard/snapshots/<file>.json"; \
		exit 2; \
	fi
	$(PY) scripts/deploy_drift_guard.py check --project-root . --output-dir outputs/deploy_guard --baseline "$(BASELINE)"

deploy-pre-sync-template: ## Generate pre-sync artifact bundle (snapshot + backup + rollback template)
	@if [ -z "$(CHANGE_ID)" ]; then \
		echo "Usage: make deploy-pre-sync-template CHANGE_ID=CHG-YYYYMMDD-XX"; \
		exit 2; \
	fi
	$(PY) scripts/deploy_drift_guard.py prepare --project-root . --output-dir outputs/deploy_guard --change-id "$(CHANGE_ID)"

release-channel-gate: ## Evaluate canary->stable release gate using latest canary/drift/pre-sync artifacts
	@if [ -z "$(CHANGE_ID)" ]; then \
		echo "Usage: make release-channel-gate CHANGE_ID=CHG-YYYYMMDD-XX"; \
		exit 2; \
	fi
	$(PY) scripts/release_channel_guard.py gate --project-root . --output-dir outputs/deploy_guard --soak-dir outputs/soak_reports --change-id "$(CHANGE_ID)" --min-trading-days 5 --max-report-age-hours 72

release-channel-promote: ## Apply stable promotion record when release gate passes
	@if [ -z "$(CHANGE_ID)" ]; then \
		echo "Usage: make release-channel-promote CHANGE_ID=CHG-YYYYMMDD-XX [ACTOR=ops]"; \
		exit 2; \
	fi
	$(PY) scripts/release_channel_guard.py promote --project-root . --output-dir outputs/deploy_guard --soak-dir outputs/soak_reports --change-id "$(CHANGE_ID)" --min-trading-days 5 --max-report-age-hours 72 --actor "$${ACTOR:-ops}" --apply

release-converge-scan: ## Generate deep inventory snapshot only (ls/tree/git/du) for release convergence
	$(PY) scripts/release_converge.py --project-root . --output-dir outputs/release_converge --tree-depth "$${TREE_DEPTH:-3}" --skip-clean --skip-gate

release-converge-clean: ## Deep clean caches/artifacts then run release gates (roadmap + targeted tests/lint)
	$(PY) scripts/release_converge.py --project-root . --output-dir outputs/release_converge --tree-depth "$${TREE_DEPTH:-3}" $(if $(filter 1,$(CLEAN_RUST)),--clean-rust,)

release-converge: release-converge-clean ## Alias: converge repository to release-ready state

release-converge-mvp: ## Aggressive MVP release convergence (full gate + minimal research sample + tracked report slimming)
	$(PY) scripts/release_converge.py --project-root . --output-dir outputs/release_converge --tree-depth "$${TREE_DEPTH:-3}" --cleanup-profile mvp_release --gate-profile full --tracked-slimming-profile root_reports_minimal --seed-minimal-sample $(if $(filter 1,$(CLEAN_RUST)),--clean-rust,)

release-first-ops-gate: ## First operational release gate (no cleanup; fail-closed)
	@if [ -z "$(CHANGE_ID)" ]; then \
		echo "Usage: HFT_ALPHA_AUDIT_ENABLED=1 make release-first-ops-gate CHANGE_ID=CHG-YYYYMMDD-XX [MONTH=YYYY-MM]"; \
		exit 2; \
	fi
	$(PY) scripts/release_first_ops_gate.py --project-root . --output-dir outputs/release_first_ops --change-id "$(CHANGE_ID)" --month "$${MONTH:-$(shell date +%Y-%m)}" --tree-depth "$${TREE_DEPTH:-2}"

release-first-ops-promote: release-first-ops-gate ## Promote stable only after first operational release gate passes
	@if [ -z "$(CHANGE_ID)" ]; then \
		echo "Usage: HFT_ALPHA_AUDIT_ENABLED=1 make release-first-ops-promote CHANGE_ID=CHG-YYYYMMDD-XX [ACTOR=ops]"; \
		exit 2; \
	fi
	$(PY) scripts/release_channel_guard.py promote --project-root . --output-dir outputs/deploy_guard --soak-dir outputs/soak_reports --change-id "$(CHANGE_ID)" --min-trading-days 5 --max-report-age-hours 72 --actor "$${ACTOR:-ops}" --apply

canary-snapshot: ## Capture pre-deploy canary baseline metrics from Prometheus
	$(PY) scripts/code_canary_gate.py snapshot --output outputs/deploy_guard/canary/baseline.json

canary-evaluate: ## Evaluate post-deploy metrics against canary baseline
	$(PY) scripts/code_canary_gate.py evaluate --baseline outputs/deploy_guard/canary/baseline.json

canary-auto: ## One-shot canary gate: snapshot + wait 5min + evaluate
	$(PY) scripts/code_canary_gate.py auto --window-s 300


reliability-monthly-pack: ## Generate monthly reliability review pack (soak/backlog/drift/disk/drill/query-guard/feature-canary/callback-latency)
	$(PY) scripts/reliability_review_pack.py --project-root . --soak-dir outputs/soak_reports --deploy-dir outputs/deploy_guard --query-guard-dir outputs/query_guard --feature-canary-dir outputs/feature_canary --callback-latency-dir outputs/callback_latency --output-dir outputs/reliability/monthly --month "$${MONTH:-$(shell date +%Y-%m)}" --disk-path . --disk-path .wal --min-query-guard-runs "$${QUERY_GUARD_MIN_RUNS:-1}" --min-query-guard-suite-runs "$${QUERY_GUARD_MIN_SUITE_RUNS:-1}" --min-feature-canary-runs "$${FEATURE_CANARY_MIN_RUNS:-1}" --min-callback-latency-runs "$${CALLBACK_LATENCY_MIN_RUNS:-1}" $(if $(filter 1,$(RUN_DRILL)),--run-drill-suite,) --allow-warn-exit-zero

roadmap-delivery-check: ## Validate TODO/ROADMAP governance (skills/RACI/agent roles/KPI) and emit execution board
	$(PY) scripts/roadmap_delivery_executor.py --project-root . --todo docs/TODO.md --roadmap ROADMAP.md --benchmark benchmark.json --paper-index research/knowledge/paper_index.json --output-dir outputs/roadmap_execution --allow-warn-exit-zero
	$(PY) scripts/roadmap_delivery_guard.py --todo docs/TODO.md --roadmap ROADMAP.md --execution-dir outputs/roadmap_execution --max-artifact-age-hours "$${MAX_ARTIFACT_AGE_HOURS:-168}" --output-dir outputs/roadmap_delivery $(if $(filter 1,$(ALLOW_WARN)),--allow-warn-exit-zero,)

roadmap-delivery-execute: ## Materialize WS-A/B/C/F/G/H deliverables (burn-in/baseline/quality/review + hotpath + source catalog)
	$(PY) scripts/roadmap_delivery_executor.py --project-root . --todo docs/TODO.md --roadmap ROADMAP.md --benchmark benchmark.json --pyspy-triage outputs/research_maintenance/pyspy_triage.json --perf-snapshot outputs/perf_gate_latency_snapshot.clean.json --stage-probe outputs/latency_stage_probe_custom_nonorder.json --paper-index research/knowledge/paper_index.json --runs-root research/experiments/runs --promotions-root research/experiments/promotions --output-dir outputs/roadmap_execution $(if $(filter 1,$(ALLOW_WARN)),--allow-warn-exit-zero,)

ch-query-guard-check: ## Guard-check ClickHouse SQL (read-only + full-scan policy)
	@if [ -z "$(QUERY)" ] && [ -z "$(QUERY_FILE)" ]; then \
		echo "Usage: make ch-query-guard-check QUERY='SELECT ...' [ALLOW_FULL_SCAN=1]"; \
		echo "   or: make ch-query-guard-check QUERY_FILE=path/to/query.sql [ALLOW_FULL_SCAN=1]"; \
		exit 2; \
	fi
	$(PY) scripts/ch_query_guard.py check \
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
	$(PY) scripts/ch_query_guard.py run \
		$(if $(QUERY),--query "$(QUERY)",--query-file "$(QUERY_FILE)") \
		--output-dir outputs/query_guard \
		$(if $(filter 1,$(ALLOW_FULL_SCAN)),--allow-full-scan,) \
		$(if $(filter 1,$(ALLOW_WARN_EXEC)),--allow-warn-execute,)

ch-query-guard-suite: ## Run baseline guarded ClickHouse query suite for periodic evidence generation
	$(PY) scripts/ch_query_guard_suite.py \
		--profile config/monitoring/query_guard_suite_baseline.json \
		--output-dir outputs/query_guard \
		--container "$${CH_CONTAINER:-clickhouse}" \
		--host "$${CH_HOST:-localhost}" \
		--port "$${CH_PORT:-9000}" \
		--user "$${CH_USER:-default}" \
		--timeout-s "$${CH_QUERY_TIMEOUT_S:-60}"

env-vars-guard: ## Verify runbook HFT_* vars are documented in env-vars reference
	$(PY) scripts/env_var_reference_guard.py --project-root . --output-dir outputs/env_var_guard

feature-canary-report: ## Evaluate feature shadow/canary guardrails from Prometheus and emit report
	$(PY) scripts/feature_canary_guard.py --prom-url "$${PROM_URL:-http://localhost:9091}" --window "$${WINDOW:-1h}" --output-dir outputs/feature_canary $(if $(filter 1,$(ALLOW_WARN)),--allow-warn-exit-zero,)

callback-latency-report: ## Evaluate Shioaji callback ingress latency/queue/parser guardrails from Prometheus
	$(PY) scripts/callback_latency_guard.py --prom-url "$${PROM_URL:-http://localhost:9091}" --window "$${WINDOW:-30m}" --output-dir outputs/callback_latency $(if $(filter 1,$(ALLOW_WARN)),--allow-warn-exit-zero,)

incident-timeline: ## Render incident timeline artifact from decision trace JSONL
	@if [ -z "$(TRACE_FILE)" ]; then \
		echo "Usage: make incident-timeline TRACE_FILE=outputs/decision_traces/<day>.jsonl [TRACE_ID=topic:seq] [FORMAT=md|json] [OUT=path]"; \
		exit 2; \
	fi
	PYTHONPATH=src $(PY) scripts/render_incident_timeline.py "$(TRACE_FILE)" $(if $(TRACE_ID),--trace-id "$(TRACE_ID)",) --format "$${FORMAT:-md}" --out "$${OUT:-outputs/incidents/timeline.$${FORMAT:-md}}"

history-repair: ## Repair fragmented historical parquet exports and resample to complete OHLCV
	@if [ -z "$(INPUTS)" ] || [ -z "$(OUT)" ]; then \
		echo "Usage: make history-repair INPUTS='data/a.parquet data/b.parquet.part' OUT=outputs/history/repaired.parquet [ARGS='--target-ms 1000 --report-out outputs/history/repaired_report.json']"; \
		exit 2; \
	fi
	$(PY) scripts/repair_history_resample.py $(foreach f,$(INPUTS),--input $(f)) --out "$(OUT)" $(ARGS)

# ============================================================================
# Research Factory
# ============================================================================

research-init: ## Initialize canonical research layout
	$(PY) -m research.factory init

research-converge-tools: ## Move non-core scripts to research/tools/legacy
	$(PY) -m research.factory converge-tools

research-clean: ## Remove research cache artifacts (__pycache__, .pyc, numba caches)
	$(PY) -m research.factory clean

research-audit: ## Audit research pipeline contract and write report
	$(PY) -m research.factory audit

research-audit-strict: ## Strict audit (--fail-on-warning) for CI compatibility
	$(PY) -m research.factory audit --fail-on-warning

research-index: ## Build machine-readable research pipeline index
	$(PY) -m research.factory index

research-optimize: ## One-flow factory optimize (init -> converge-tools -> clean -> audit -> index)
	$(PY) -m research.factory optimize

research: ## Official single entrance: strict pipeline with factory optimize preflight
	@if [ -z "$(ALPHA)" ] || [ -z "$(OWNER)" ] || [ -z "$(DATA)" ]; then \
		echo "Usage: make research ALPHA=<alpha_id> OWNER=<owner> DATA='<path1.npy [path2.npy ...]' [ARGS='--min-sharpe-oos-gate-d 1.2']"; \
		exit 2; \
	fi
	$(PY) -m research.pipeline run --alpha-id "$(ALPHA)" --owner "$(OWNER)" --data $(DATA) $(ARGS)

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
	$(PY) -m research.pipeline triage --alpha-id "$(ALPHA)" --owner "$(OWNER)" --data $(DATA) $(ARGS)

research-scaffold: ## Scaffold a new governed alpha package under research/alphas/
	@if [ -z "$(ALPHA)" ]; then \
		echo "Usage: make research-scaffold ALPHA=<alpha_id> [ARGS='--paper ref --complexity O1']"; \
		exit 2; \
	fi
	$(PY) -m research scaffold $(ALPHA) $(ARGS)

research-report: ## Render promotion report markdown for a given alpha
	@if [ -z "$(ALPHA)" ]; then \
		echo "Usage: make research-report ALPHA=<alpha_id> [ARGS='--out report.md']"; \
		exit 2; \
	fi
	$(PY) research/tools/render_promotion_report.py --alpha-id "$(ALPHA)" $(ARGS)

research-fetch-paper: ## Fetch and index an arxiv paper (e.g. make research-fetch-paper ARXIV=2408.03594)
	@if [ -z "$(ARXIV)" ]; then \
		echo "Usage: make research-fetch-paper ARXIV=<arxiv_id>"; \
		exit 2; \
	fi
	$(PY) -m research fetch-paper "$(ARXIV)" $(ARGS)

research-search-papers: ## Search arxiv papers (e.g. make research-search-papers QUERY=\"order flow imbalance\")
	@if [ -z "$(QUERY)" ]; then \
		echo "Usage: make research-search-papers QUERY=\"<search terms>\""; \
		exit 2; \
	fi
	$(PY) -m research search-papers "$(QUERY)" $(ARGS)

research-paper-prototype: ## Scaffold prototype directly from paper ref in paper_index
	@if [ -z "$(PAPER_REF)" ]; then \
		echo "Usage: make research-paper-prototype PAPER_REF=<ref|arxiv_id> [ARGS='--alpha-id my_alpha --complexity O1']"; \
		exit 2; \
	fi
	$(PY) -m research paper-to-prototype "$(PAPER_REF)" $(ARGS)

research-record-paper: ## Record one paper-trade session
	@if [ -z "$(ALPHA)" ]; then \
		echo "Usage: make research-record-paper ALPHA=<alpha_id> [ARGS='--trading-day 2026-02-28 --fills 20 --pnl-bps 4.2']"; \
		exit 2; \
	fi
	$(PY) -m research record-paper --alpha-id "$(ALPHA)" $(ARGS)

research-summarize-paper: ## Summarize paper-trade sessions for one alpha
	@if [ -z "$(ALPHA)" ]; then \
		echo "Usage: make research-summarize-paper ALPHA=<alpha_id> [ARGS='--out outputs/paper_summary.json']"; \
		exit 2; \
	fi
	$(PY) -m research summarize-paper --alpha-id "$(ALPHA)" $(ARGS)

research-check-paper-governance: ## Check Gate-E paper-trade governance readiness
	@if [ -z "$(ALPHA)" ]; then \
		echo "Usage: make research-check-paper-governance ALPHA=<alpha_id> [ARGS='--strict --out outputs/paper_governance.json']"; \
		exit 2; \
	fi
	$(PY) -m research check-paper-governance --alpha-id "$(ALPHA)" $(ARGS)

research-gen-synth-lob: ## Generate synthetic LOB dataset + metadata sidecar
	@if [ -z "$(OUT)" ]; then \
		echo "Usage: make research-gen-synth-lob OUT=research/data/processed/<name>.npy [ARGS='--version v2 --rng-seed 42 --symbols TXF,MXF --split train']"; \
		exit 2; \
	fi
	$(PY) research/tools/synth_lob_gen.py --out "$(OUT)" $(ARGS)

research-stamp-data-meta: ## Create data metadata sidecar for dataset
	@if [ -z "$(DATA_PATH)" ]; then \
		echo "Usage: make research-stamp-data-meta DATA_PATH=<path.npy|path.npz> [ARGS='--source-type real --owner charlie --symbols 2330']"; \
		exit 2; \
	fi
	$(PY) -m research stamp-data-meta "$(DATA_PATH)" $(ARGS)

research-validate-data-meta: ## Validate data metadata sidecar for dataset
	@if [ -z "$(DATA_PATH)" ]; then \
		echo "Usage: make research-validate-data-meta DATA_PATH=<path.npy|path.npz> [ARGS='--meta <meta.json>']"; \
		exit 2; \
	fi
	$(PY) -m research validate-data-meta "$(DATA_PATH)" $(ARGS)


research-batch-correlation: ## Batch compute pool correlations across all alpha scorecards
	$(PY) -m hft_platform alpha batch-correlation $(ARGS)

research-paper-trade-batch: ## Batch paper-trade session management (discover/record)
	$(PY) -m hft_platform alpha paper-trade-batch $(ARGS)

research-promote-batch: ## Batch run promotion pipeline across alphas (dry-run by default)
	$(PY) -m hft_platform alpha promote-batch $(ARGS)

research-batch-search: ## Batch search arXiv papers (QUERIES="query1;query2;query3")
	@if [ -z "$(QUERIES)" ]; then \
		echo "Usage: make research-batch-search QUERIES=\"order flow imbalance;market microstructure\""; \
		exit 2; \
	fi
	$(PY) -m research batch-search $(subst ;, ,$(QUERIES)) $(ARGS)

research-hypothesis-ingest: ## Ingest hypotheses from paper index into queue
	$(PY) -m research hypothesis-queue ingest $(ARGS)

research-hypothesis-top: ## Show top-N pending hypotheses (default N=5)
	$(PY) -m research hypothesis-queue top $(ARGS)

research-auto-scaffold: ## Auto-scaffold top-N alpha packages from hypothesis queue
	$(PY) -m research auto-scaffold $(ARGS)

experiment-gc: ## Delete experiment artifacts older than 90 days (keep latest 3 per alpha)
	uv run python scripts/experiment_gc.py

experiment-gc-dry-run: ## Dry-run experiment GC (print what would be deleted)
	uv run python scripts/experiment_gc.py --dry-run

hotpath-profile: ## Profile per-stage latency: normalizer → LOB → feature → strategy → risk (10k iterations)
	uv run python scripts/latency/hotpath_profile_matrix.py

quarterly-health-check: ## Run quarterly infrastructure health check
	uv run python scripts/quarterly_health_check.py

# ============================================================================
# Help
# ============================================================================

help: ## Show this help message
	@echo "HFT Platform Development Commands"
	@echo ""
	@echo "Usage: make [target]"
	@echo ""
	@grep -E '^[a-zA-Z0-9_.-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
