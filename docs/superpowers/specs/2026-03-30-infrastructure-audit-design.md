# Infrastructure Audit & Upgrade Roadmap — Design Spec

**Date:** 2026-03-30
**Scope:** Ops Automation, CI/CD Pipeline, Monitoring/Alerting, Config Management
**Approach:** Tiered remediation roadmap (P0→P3), prioritized by severity
**Total findings:** 56 items across 4 domains

---

## 1. Audit Summary

### Methodology

Four parallel deep scans across:
1. **Ops Automation** — 70+ scripts in `scripts/`, `ops.sh`, Makefile ops targets, cron scheduling
2. **CI/CD Pipeline** — 4 GitHub Actions workflows, `.pre-commit-config.yaml`, Makefile CI targets
3. **Monitoring/Alerting** — ~110 exported metrics, 53 alert rules, 5 Grafana dashboards, Prometheus/Alertmanager/Loki configs
4. **Config Management** — `config/base/` + `config/env/` merge chain, `loader.py`, schema validation, hot-reload

### Severity Distribution

| Phase | Severity | Items | Domains |
|-------|----------|-------|---------|
| P0 | CRITICAL | 12 | Monitoring 6 / Ops 3 / Config 2 / CI 1 |
| P1 | HIGH | 15 | Monitoring 8 / CI 4 / Config 2 / Ops 1 |
| P2 | MEDIUM | 18 | CI 8 / Monitoring 4 / Config 4 / Ops 2 |
| P3 | LOW | 11 | Ops 6 / CI 3 / Config 2 |

---

## 2. P0 — CRITICAL: Fix Broken Things

**Exit criteria:** All CRITICAL items fixed + verified (dashboard shows data, alerts can fire, backup is scheduled).

### Monitoring

#### M-01: Production dashboard uses non-existent metric namespace
- **File:** `config/monitoring/dashboards/hft-production.json`
- **Problem:** All 10 panels reference `hft_*`-prefixed metrics (`hft_storm_guard_state`, `hft_feed_ticks_total`, `hft_orders_submitted_total`, etc.) that do not exist in `metrics.py`. Actual names: `stormguard_mode`, `feed_events_total`, `order_actions_total`, etc.
- **Impact:** Main production dashboard shows "No data" on every panel.
- **Fix:** Rewrite all panel queries to use actual metric names from `metrics.py`. Also fix datasource uid from `"prometheus"` to `"Prometheus"` (case mismatch with provisioned datasource name in `datasource.yml`). Standardize datasource uid across all 5 dashboard files.

#### M-02: YAML indentation bug silences 3 alert rules
- **File:** `config/monitoring/alerts/rules.yaml`, lines 509–536
- **Problem:** `ResearchDataTooLarge`, `SSDReallocatedSectorsHigh`, `SSDWearLevelLow` are nested under the `BackupStale` rule block due to incorrect 4-space indentation instead of top-level `rules:` list indentation.
- **Impact:** Three alerts silently ignored or cause parse error.
- **Fix:** De-indent the three rules to align with sibling rules at the `rules:` list level.

#### M-03: Alertmanager inhibit rules reference wrong alert names
- **File:** `config/monitoring/alerts/alertmanager.production.yml`
- **Problem:** Inhibit rules reference `HFTStormGuardHalt` (actual: `StormGuardHalt`), `ClickHouseDown` (actual: `ClickHouseConnectionDown`), `FeedDead` (no such alert; closest: `FeedGapCritical`).
- **Impact:** All three inhibition rules are dead — never fire.
- **Fix:** Update inhibit rule `alertname` values to match actual alert names in `rules.yaml`.

#### M-04: Redis monitoring completely non-functional
- **Problem:** `RedisConnectionDown` alert uses `redis_connection_health` metric which is not exported by `metrics.py` or any exporter. No `redis_exporter` scrape job in `prometheus.yml`.
- **Impact:** Redis liveness has zero working coverage.
- **Fix:** Either (a) add `redis_connection_health` gauge to `metrics.py` and set it from the Redis client health check path, or (b) deploy `redis_exporter` sidecar and add scrape job. Option (a) is simpler and consistent with the `clickhouse_connection_health` pattern.

#### M-05: No ClickHouse exporter scrape job
- **File:** `config/monitoring/prometheus.yml`
- **Problem:** `ClickHouseSystemLogSizeCritical` alert uses `clickhouse_table_parts_bytes` from ClickHouse's native Prometheus exporter (port 9363), but no scrape job exists for it.
- **Impact:** Table-level ClickHouse metrics unavailable. This is the same class of issue that caused the 2026-03-02 disk crisis (system logs growing to 104 GB undetected).
- **Fix:** Add scrape job `job_name: "clickhouse"` targeting `clickhouse:9363` in `prometheus.yml`. Verify ClickHouse's `prometheus.port` config is enabled in `docker-compose.yml`.

#### M-06: No TargetDown alert for hft-engine
- **Problem:** If `hft-engine` process crashes cleanly, no direct `up == 0` alert fires. Indirect coverage exists only via stale liveness gauges, which have varying staleness thresholds.
- **Fix:** Add alert rule:
  ```yaml
  - alert: HFTEngineDown
    expr: up{job="hft-engine"} == 0
    for: 30s
    labels: { severity: critical }
    annotations: { summary: "HFT Engine scrape target is down" }
  ```

### Ops

#### O-01: `scripts/release_readiness.py` does not exist
- **File:** Referenced by `Makefile` target `release-readiness-check`
- **Problem:** The file is missing. `make release-readiness-check` fails immediately. The target references milestone date 2026-03-30 (today).
- **Fix:** Either (a) create a stub that delegates to `release_converge.py` with appropriate flags, or (b) remove the Makefile target until the script is implemented. Option (b) is safer — do not leave a broken target.

#### O-02: ClickHouse backup not scheduled in cron
- **Problem:** Two backup scripts exist (`clickhouse_backup.sh` at 14:30 per design spec, `daily-backup.sh` at 17:00 per P1 plan). Neither appears in `docs/operations/cron-setup-remote.md` canonical crontab. No repository-backed evidence that either script is scheduled on the remote host.
- **Impact:** If no cron entry exists on the remote host, ClickHouse data has no nightly backup. Verify on remote host before assuming worst case.
- **Fix:** Add `clickhouse_backup.sh` to `cron-setup-remote.md` at the design-spec time (14:30 daily). Document `daily-backup.sh` as the local-dev alias or retire it (see P2 O-05).

#### O-03: `verify_rust_deployment.py` imports non-existent module
- **Problem:** Imports `research.tools.factor_registry` which does not exist. Script will crash on any invocation.
- **Fix:** Delete the script (orphan, no Makefile/CI/doc references).

### Config

#### C-01: Prod risk/canary/strategy configs not auto-loaded
- **Files:** `config/env/prod/risk.yaml`, `config/env/prod/canary.yaml`, `config/env/prod/strategies.yaml`
- **Problem:** `config/loader.py` merge chain only reads `{env}/main.yaml`. These three prod-specific files exist but are never loaded by the standard path. The prod risk limits (10K NTD halt threshold, max 1 lot, etc.) may not be applied.
- **Impact:** Production may run with base defaults instead of prod-specific risk parameters.
- **Fix:** Investigate how `bootstrap.py` resolves `risk_path` and `strategy_path`. If these files are loaded via a separate mechanism, document it explicitly. If not, either (a) extend `loader.py` to glob `{env}/*.yaml` and merge all, or (b) inline the critical prod values into `env/prod/main.yaml`. Option (b) is simpler and avoids changing the loader contract.

#### C-02: `HFT_MODE="real"` validation mismatch
- **Problem:** `config/schema.py` `_VALID_MODES = {"sim", "live", "replay"}` rejects `"real"`. But `bootstrap.py` accepts `"real"` (line 186: `"FATAL: HFT_ORDER_MODE=live requires HFT_MODE=real or live"`).
- **Fix:** Add `"real"` to `_VALID_MODES` in schema, OR normalize `"real"` → `"live"` early in bootstrap before validation. The latter is cleaner — `"real"` becomes an alias.

### CI/CD

#### CI-01: GitHub Actions not pinned to SHA digests
- **Problem:** All actions use mutable version tags (`@v4`, `@v5`, `@stable`). A compromised upstream action could inject code into CI.
- **Affected actions:** `actions/checkout`, `actions/cache`, `actions/upload-artifact`, `actions/setup-python`, `astral-sh/setup-uv`, `dtolnay/rust-toolchain`, `docker/login-action`, `docker/setup-buildx-action`, `docker/build-push-action`
- **Fix:** Pin every action to its current SHA digest. Add comment with version for readability:
  ```yaml
  uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
  ```
- **Maintenance:** Add `.github/dependabot.yml` for `github-actions` ecosystem (see P2 CI-13).

---

## 3. P1 — HIGH: Close Key Gaps

**Exit criteria:** All HIGH items resolved + new alerts/dashboards verified in Prometheus/Grafana.

### Monitoring

#### M-07: No alert on fill loss (`exec_overflow_evicted_total`)
- **Fix:** Add alert rule for `rate(exec_overflow_evicted_total[5m]) > 0` with severity `critical`. Any evicted fill is data loss and must page immediately.

#### M-08: SLO-2 Order-to-Fill Latency unmeasured
- **Problem:** `order_to_fill_latency_ms` referenced in SLO doc but not exported.
- **Fix:** Implement the metric in the execution path (order timestamp → fill callback timestamp delta), export as Histogram, add recording rule for P95, add alert for SLO burn rate.

#### M-09: Alpha signal silence unalerted
- **Fix:** Add alert: `time() - alpha_last_signal_ts > 300` → `AlphaSignalSilent`. The metric and the intended alert logic already exist as a code comment in `metrics.py`.

#### M-10: Autonomy control-plane state changes unalerted
- **Fix:** Add alerts for:
  - `strategy_quarantine_active == 1` → `StrategyQuarantineActive`
  - `platform_reduce_only_active == 1` → `PlatformReduceOnlyActive`
  - `manual_rearm_required == 1` → `ManualRearmRequired`
  - `increase(autonomy_transitions_total[5m]) > 0` → `AutonomyStateTransition` (warning)

#### M-11: Gateway heartbeat staleness unalerted
- **Fix:** Add alert mirroring `ExecutionRouterHeartbeatStale` for gateway:
  ```yaml
  - alert: ExecutionGatewayHeartbeatStale
    expr: time() - execution_gateway_heartbeat_ts > 60
  ```

#### M-12: WAL corruption/replay errors unalerted
- **Fix:** Add two alerts:
  - `increase(wal_corrupt_files_total[1h]) > 0` → `WALCorruptionDetected` (critical)
  - `increase(wal_replay_errors_total[1h]) > 0` → `WALReplayErrorsDetected` (warning)

#### M-13: ClickHouse pool exhaustion unalerted
- **Fix:** Add alert: `increase(clickhouse_pool_timeout_total[5m]) > 0` → `ClickHousePoolExhausted`

#### M-14: Missing dashboards for Pipeline/Strategy/Risk planes
- **Fix:** Create 3 new Grafana dashboards:
  - **Pipeline Health:** `bus_overflow_total`, `queue_depth`, `event_loop_lag_ms`, `raw_queue_depth`, `normalization_errors_total`
  - **Strategy & Risk:** `strategy_latency_ns`, `strategy_intents_total`, `risk_reject_total`, `stormguard_mode`, `autonomy_mode`, `strategy_quarantine_active`, `alpha_last_signal_ts`
  - **Execution & Order:** `execution_router_lag_ns`, `order_actions_total`, `order_reject_total`, `reconciliation_discrepancy_count`, `position_pnl_realized`, `portfolio_drawdown_pct`

### CI/CD

#### CI-02: No container image scanning
- **Fix:** Add `aquasecurity/trivy-action` step in `deploy.yml` after `docker build-push-action`, before SSH deploy. Fail on HIGH+ CVE.

#### CI-03: No `cargo audit`
- **Fix:** Add step in `ci.yml` `rust` job: `cargo install cargo-audit && cargo audit`. Or use `actions-rs/audit-check` action.

#### CI-04: Weak secret scanning
- **Fix:** Replace inline regex with `gitleaks/gitleaks-action` in `ci.yml`. Scan entire repo (not just `src/`).

#### CI-05: Missing `timeout-minutes` on 8 jobs
- **Fix:** Add `timeout-minutes` to each job:

  | Job | Timeout |
  |-----|---------|
  | `lint` | 15 |
  | `rust` | 20 |
  | `typecheck` | 10 |
  | `dependency-boundary` | 5 |
  | `benchmark` | 30 |
  | `latency-gate` | 20 |
  | `integration` | 30 |
  | `security` | 10 |

### Config

#### C-03: Schema validates only 7 top-level keys
- **Problem:** `risk`, `stormguard`, `circuit_breaker`, `rate_limit`, `canary`, `shadow`, `shioaji` sub-configs all pass through unvalidated. A typo like `stormgurad` is silently ignored.
- **Fix:** Extend `config/schema.py` to validate known sub-config schemas. Use `pydantic` or `msgspec.Struct` for nested validation. At minimum: risk limits (soft < hard), stormguard thresholds (positive numbers), rate limits (positive ints).

#### C-04: Hot-reload has no schema validation
- **Problem:** `ConfigWatcher` calls `yaml.safe_load()` with no validation. Malformed YAML silently becomes `{}`.
- **Fix:** Add the same schema validation used at startup to the hot-reload path. Reject invalid reloads with a structlog warning and keep the previous valid config.

### Ops

#### O-04: `ops.sh` `clean` subcommand not implemented
- **Fix:** Either implement the `clean)` case (remove `/tmp/ptp4l.conf`, `__pycache__/`, `.benchmarks/`, etc.) or remove `clean` from the usage header comment. Prefer removing from header — `make clean` is the standard path.

---

## 4. P2 — MEDIUM: Structural Improvements

**Exit criteria:** CI pipeline efficiency measurably improved, config layer cleaned up, monitoring coverage gaps addressed.

### CI/CD

#### CI-06: Setup boilerplate duplication (9 jobs)
- **Fix:** Extract `.github/actions/setup-python-uv/action.yml` and `.github/actions/setup-rust/action.yml` composite actions. Replace 9 copy-pasted setup blocks with single-line `uses: ./.github/actions/setup-python-uv`.

#### CI-07: `canary-deploy.yml` uses bare pip
- **Fix:** Replace `pip install structlog pyyaml` with `astral-sh/setup-uv` + `uv sync --dev` (or `uv pip install structlog pyyaml` at minimum).

#### CI-08: `latency-gate` bypasses Makefile
- **Fix:** Add `latency-gate-ci` Makefile target wrapping the same `pytest tests/bench/ -m bench` invocation. Update `ci.yml` to call `make latency-gate-ci`.

#### CI-09: Divergent print/float checks between workflows
- **Fix:** Consolidate into `ci.yml` only. Remove duplicate checks from `pr-review-checklist.yml` or make both reference a shared script in `scripts/`.

#### CI-10: No CodeQL/semgrep SAST
- **Fix:** Add `github/codeql-action` workflow for Python. Consider `semgrep` with `p/python` + `p/security-audit` rulesets.

#### CI-11: No license compliance check
- **Fix:** Add step: `uv run pip-licenses --fail-on-licenses "GPL-2.0,GPL-3.0"` in the `security` job.

#### CI-12: No `concurrency` on PR runs
- **Fix:** Add to `ci.yml`:
  ```yaml
  concurrency:
    group: ci-${{ github.ref }}
    cancel-in-progress: ${{ github.event_name == 'pull_request' }}
  ```

#### CI-13: No Dependabot configuration
- **Fix:** Create `.github/dependabot.yml` for `github-actions` (weekly) and `pip` (weekly) ecosystems.

### Monitoring

#### M-15: 76 orphaned metrics
- **Fix:** Triage into three buckets: (a) wire to new dashboards from M-14, (b) wire to new alert rules, (c) document as intentionally unmonitored (development/debug metrics). Target: reduce orphan count to < 20.

#### M-16: SMARTmon exporter not scraped
- **Fix:** Add `smartmon` textfile collector to `node-exporter` (via `--collector.textfile.directory=/etc/node-exporter/`). The `scripts/smart_check.sh` already writes Prometheus textfile format — wire it to node-exporter's textfile directory.

#### M-17: 43/53 alerts missing `runbook_url`
- **Fix:** For the 9 existing runbooks, add `runbook_url: https://github.com/<repo>/blob/main/docs/runbooks/<AlertName>.md` annotation to their corresponding alert rules. For the remaining 34, create stub runbooks or link to the closest existing runbook.

#### M-18: Production alertmanager config not loaded
- **Fix:** Evaluate whether the Telegram-only routing is intentional. If PagerDuty/Slack differentiation is desired, switch `docker-compose.yml` alertmanager volume mount to use `alertmanager.production.yml`. If not, remove the unused production config to avoid confusion.

### Config

#### C-05: Dead `config/base/fees/futures.yaml`
- **Fix:** Verify no read path exists. If confirmed dead, delete. Fee data is already in `symbols.yaml` per-symbol.

#### C-06: Root-level orphan config files
- **Fix:** Delete `config/risk.yaml`, `config/execution.yaml`, `config/recorder.yaml`, `config/strategies.yaml`, `config/strategy_limits.yaml` (root-level copies). These are pre-migration leftovers; authoritative versions are in `config/base/`.

#### C-07: `CLICKHOUSE_USERNAME` deprecated alias still active
- **Fix:** In `recorder/writer.py`, `recorder/_loader_ch.py`, `monitor/_config_loader.py` — log a deprecation warning when `CLICKHOUSE_USERNAME` is used, advising migration to `HFT_CLICKHOUSE_USER`. Remove in next major version.

#### C-08: `dev/sim/staging` environments identical
- **Fix:** Either (a) differentiate staging with meaningful overrides (e.g., `log_level: debug`, `recorder_mode: wal_first`), or (b) remove `dev/` and `staging/` directories and document that `sim/` is the default non-production environment.

### Ops

#### O-05: Duplicate backup scripts
- **Fix:** Designate `clickhouse_backup.sh` as canonical (per design spec). Retire `daily-backup.sh` — either delete or rename to `daily-backup.sh.deprecated` with a comment pointing to canonical script.

#### O-06: `post_market_check.py` relative WAL path
- **Fix:** Use `Path(__file__).resolve().parent.parent / ".wal"` (same pattern as `pre_market_check.py`) instead of relative `.wal`.

---

## 5. P3 — LOW: Clean Up Technical Debt

**Exit criteria:** `scripts/` directory lean, dead code removed, documentation aligned.

### Ops

#### O-07: 17 orphan scripts from 2026-02-04
- **Scripts:** `agent_session.sh`, `batch_create_snapshots.py`, `create_snapshot.py`, `debug_hbt_init.py`, `debug_lob_minimal.py`, `inspect_npz.py`, `inspect_snapshot_sig.py`, `latency_e2e_report.py` (root), `live_contract_cache_refresh.py`, `patch_trade_side.py`, `run_paper_trading.sh`, `shioaji_latency_probe.py` (root), `sim_full_pipeline.py`, `sim_futures_strategy_order.py`, `sim_shioaji_futures_smoke.py`, `sim_shioaji_order_smoke.py`, `sim_shioaji_stock_diag.py`
- **Fix:** Delete all 17 confirmed orphans (O-03 already removes `verify_rust_deployment.py`; `latency_e2e_report.py` root copy superseded by `latency/` version). Create a single commit: `chore: remove 17 orphaned scripts from Feb 2026 bootstrap phase`.

#### O-08: `ops.sh` `monitor-ch` stale
- **Fix:** Remove subcommand and update header/usage text. `make recorder-status` is the replacement.

#### O-09: `ops.sh` `test` redundant with `make test`
- **Fix:** Remove subcommand and update header.

#### O-10: `ops.sh` deprecated sysctl `tcp_low_latency`
- **Fix:** Remove `net.ipv4.tcp_low_latency` from `cmd_tune()`. Also remove the corresponding check from `host_preflight.sh`.

#### O-11: `deploy.sh` hardcoded paths
- **Fix:** Parameterize `REMOTE_DIR` with a mandatory env var (no default fallback). Parameterize health check URL with `DEPLOY_HEALTH_URL`.

#### O-12: `daily-backup.sh` force-enables backup
- **Fix:** Resolved by O-05 (retire script). If kept, respect `HFT_BACKUP_ENABLED` from environment.

### CI/CD

#### CI-14: Redundant Rust builds in 4 jobs
- **Fix:** Upload compiled `.so` as artifact from `test` job. Downstream jobs (`benchmark`, `latency-gate`, `integration`, `recorder-nightly-drills`) download instead of rebuilding. Saves ~2 min per job.

#### CI-15: SSH key copy-paste in `deploy.yml`
- **Fix:** Extract to composite action `.github/actions/ssh-deploy/action.yml` with guaranteed key cleanup in `post` step. Or use `webfactory/ssh-agent` action.

#### CI-16: Darwin benchmark baseline never auto-updated
- **Fix:** Add post-merge step on `main` that downloads benchmark artifact and commits updated `.benchmark_baseline.json` if regression threshold passes.

### Config

#### C-09: Dead config files (`system.json`, `settings.json`, `exported_settings.json`)
- **Fix:** Delete `config/system.json` and `config/settings.json`. Add `config/exported_settings.json` to `.gitignore`.

#### C-10: CLAUDE.md env var table coverage gap
- **Fix:** Not blocking. Run `scripts/env_var_reference_guard.py` to generate a full inventory. Update CLAUDE.md with the top 20 most operationally important missing vars. The remaining ~200 are internal/derived.

---

## 6. Verification Plan

### Per-Phase Verification

| Phase | Verification Method |
|-------|-------------------|
| P0 | Manual: load Grafana dashboard → confirm panels show data. Trigger test alert → confirm Telegram delivery. O-01: if target removed, verify `make release-readiness-check` is absent from Makefile; if stubbed, verify it exits 0. Verify cron entry for backup on remote host. |
| P1 | Deploy to staging → verify new alerts fire on synthetic conditions. Review new dashboards with real market data. Run `make ci` locally → confirm all gates pass. |
| P2 | Measure CI run time before/after. Verify `concurrency` cancels stale PR runs. Run config loader with intentional typo → confirm rejection. |
| P3 | `ls scripts/ | wc -l` should decrease by ~17. `ops.sh` usage text matches implemented subcommands. `config/` has no orphan files. |

### Rollback

Each phase is independently committable. If a phase introduces regressions:
1. Revert the phase's commits (`git revert`)
2. The previous phase's state is stable
3. No cross-phase dependencies

---

## 7. Dependencies and Ordering

```
P0 (no dependencies — all independent fixes)
├── M-01 (dashboard rewrite + datasource fix)
├── M-02..M-06 (alert fixes, independent)
├── O-01..O-03 (ops fixes, independent)
├── C-01..C-02 (config fixes, independent)
└── CI-01 (SHA pinning, independent)

P1 (depends on P0 for monitoring foundation)
├── M-07..M-13 (new alerts, after M-02 YAML fix)
├── M-14 (new dashboards, after M-01 namespace fix)
├── CI-02..CI-05 (CI gates, independent)
├── C-03..C-04 (schema validation, after C-01 loader investigation)
└── O-04 (ops.sh fix, independent)

P2 (depends on P1 for CI/monitoring stability)
├── CI-06..CI-13 (CI improvements, independent of each other)
├── M-15..M-18 (monitoring improvements, after M-14 dashboards exist)
├── C-05..C-08 (config cleanup, after C-01 loader is understood)
└── O-05..O-06 (ops cleanup, independent)

P3 (depends on P2 for clean baseline)
├── O-07..O-12 (ops debt, independent)
├── CI-14..CI-16 (CI debt, after CI-06 composite actions exist)
└── C-09..C-10 (config debt, independent)
```

---

## 8. Out of Scope

The following areas were scanned but explicitly excluded from this spec:

- **Testing infrastructure** (580 test files, 904 zero-assertion tests, coverage gaps) — separate initiative
- **Rust hot-path expansion** (FeatureEngine v3 promotion, FFI coverage) — separate initiative
- **Shioaji adapter burn-in** — operational, not infrastructure
- **Alpha governance pipeline** — research tooling, not ops infrastructure
- **Monitor TUI** (`src/hft_platform/monitor/`) — application code, not infrastructure
