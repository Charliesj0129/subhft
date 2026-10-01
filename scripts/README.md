# scripts/

Ops, CI-gate and one-off tooling. Most entries are wired into the `Makefile`
(`make -n <target>` shows the exact invocation), CI workflows, git hooks or
`docs/runbooks/`. **Paths in this directory are referenced from those places;
rename or move a script only together with every reference**
(`git grep -F scripts/<name>`).

Python code that belongs to the platform runtime lives in `src/hft_platform/`,
not here. Research tooling lives in `research/`.

## Quality and governance gates (CI / pre-commit)

| Script | Purpose |
|---|---|
| `check_discipline.py` | HFT discipline enforcement (AST gates). Protected: do not edit casually |
| `arch_conformance_gate.py` | Architecture conformance gate |
| `check_agent_docs.py` | Agent-docs consistency gate |
| `check_coverage_domains.py` | Domain-weighted coverage gate |
| `check_test_assertions.py`, `check_test_collection.py`, `check_test_naming.py`, `check_test_quality_patterns.py` | Test hygiene gates |
| `benchmark_gate.py` | Benchmark regression checker |
| `env_var_reference_guard.py` | Keeps `docs/operations/env-vars-reference.md` covering runbook `HFT_*` variables |
| `check_git_preconditions.sh` | Agent-workflow git safety pre-checks |
| `git_bundle_backup.py` | Local git-bundle backup (fail-closed) |
| `git-hooks/`, `hooks/` | Git pre-push hook and Claude Code task hooks |

## Release, deploy and canary

| Script | Purpose |
|---|---|
| `release_readiness.py`, `release_converge.py`, `release_channel_guard.py`, `release_first_ops_gate.py` | Release readiness, convergence and canary-to-stable gating |
| `auto_release_orchestrator.py` | Canary release orchestrator, evaluates promotion readiness on merge |
| `code_canary_gate.py`, `feature_canary_guard.py`, `callback_latency_guard.py` | Post-deploy metric comparison and Prometheus-based guards |
| `deploy_drift_guard.py` | Deployment drift checker and pre-sync artifact helper |
| `rollback_drill.py` | Verifies the rollback procedure works |
| `soak_acceptance.py` | Daily, weekly and canary soak acceptance reports |
| `roadmap_delivery_executor.py`, `roadmap_delivery_guard.py` | TODO/ROADMAP delivery execution and guard |
| `ops/generate_locked_compose.py` | Generates the locked production compose file |

## Daily / periodic operations

| Script | Purpose |
|---|---|
| `pre_market_check.py`, `post_market_check.py` | Pre-market health check and post-market daily check |
| `daily_reconcile.py` | Post-market 3-way PnL/position reconciliation |
| `shadow_daily_report.py`, `weekly_summary.py`, `quarterly_health_check.py`, `reliability_review_pack.py` | Daily, weekly, quarterly and monthly reports |
| `monitor_runtime_health.py`, `run_signal_monitor.sh`, `alert_test.py` | Runtime monitoring and alert-path test |
| `_notify.sh` | Shared Telegram notification helper for the shell scripts |
| `host_preflight.sh`, `host_health_report.sh`, `host_security_update.sh`, `secret_age_check.sh`, `smart_check.sh`, `validate_env.sh` | Host baseline, health, security and `.env` checks |

## Data, ClickHouse and WAL

| Script | Purpose |
|---|---|
| `clickhouse_backup.sh`, `clickhouse_restore.sh`, `clickhouse_restore_verify.sh` | Backup, disaster-recovery restore, restore verification |
| `ch_query_guard.py`, `ch_query_guard_suite.py` | ClickHouse query guard and batch baseline runner |
| `wal_dlq_ops.py`, `wal-replay-drill.sh` | WAL DLQ status/replay and the replay drill |
| `backfill_historical_ticks.py`, `sync_market_data_archive.py`, `refresh_options_symbols.py`, `repair_history_resample.py` | Data backfill, archive sync, symbol refresh, history repair |
| `research_data_rotate.sh`, `experiment_gc.py` | Research data tier rotation and experiment artifact GC |
| `migrations/` | One-off SQL migrations (schema source of truth is `src/hft_platform/migrations/clickhouse/`) |
| `render_incident_timeline.py`, `run-chaos-drill.sh`, `reconnect-burn-in-report.sh` | Incident timeline rendering, chaos drill, reconnect burn-in report |

## Research one-offs

`a1_day_decomposition.py`, `compare_r47_latency.py`, `sweep_r47_spread.py`,
`generate_q_hat_fixtures.py`, `generate_slice_d_signal_corpus.py`,
`migrate_alpha_manifests.py`. These produced specific research results or test
fixtures and are kept for reproducibility. Do not run them against production.

## Sub-packages

| Directory | Purpose |
|---|---|
| `shioaji_api_diff/` | SDK surface capture/diff/classify tooling behind `make shioaji-diff`, `shioaji-watch`, `shioaji-surface-regen` |
| `shioaji_153_harness/` | Shioaji SDK harness (venv bootstrap, phased sim soak) |
| `latency/` | Latency probes and hot-path profiling |
| `agent_routines/` | Runner for scheduled agent routines |
| `ops/` | Production-host maintenance and audit helpers |
