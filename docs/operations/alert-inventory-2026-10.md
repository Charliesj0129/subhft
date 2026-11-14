# Alert Inventory (2026-10)

Static check of every Prometheus alert shipped in the repo: does the series each alert reads exist, and what produces it?
Snapshot of `origin/main` at `53f8b815`. Nothing here was checked against a running Prometheus.

Status words: `VERIFIED` (read in source or command output), `NOT VERIFIED` (could not be checked here).

## 1. Result in one table

| Class | Alerts | Meaning |
|---|---|---|
| Series defined by the engine's metrics registry | 87 | The name an alert reads is a metric the engine defines. Says nothing about whether it is ever updated (see §4). |
| Series from a recording rule | 2 | `slo:feed_availability:ratio_rate5m`, `slo:order_success:ratio_rate5m`. Those rules are not loaded by `prometheus.yml` (§3, F2). |
| Series from Prometheus or node-exporter | 4 | `up`, `node_filesystem_*`. |
| Series **not** in the engine registry | 10 | Findings F1-F4 below. |
| Total | 103 | 47 critical, 55 warning, 1 info. 98 in `rules.yaml`, 5 in `replay_parity_alert.yml`. |

Of the 10: **one is a defect** (an alert reads a name that is never exported), **five never load**, and **four depend on a
producer outside the engine** (a cron script via node-exporter's textfile collector, or a ClickHouse exporter).

## 2. Method

1. Parse every `groups[].rules[]` entry with `alert:` in `config/monitoring/alerts/*.y*ml`
   (`rules_test.yaml` skipped) and every `record:` in `config/monitoring/recording_rules/*.y*ml`.
2. Tokenise each PromQL `expr`: strip strings, `[range]` selectors and `by/without/on/ignoring/group_*` clauses,
   skip function and aggregation names, keep selector names and their `{...}` matchers. A heuristic, not a PromQL parser.
3. Parse every `Counter`/`Gauge`/`Histogram`/`Summary`/`Info`/`Enum` constructor under `src/hft_platform/` with `ast`
   (not only `metrics.py`: `quote_connection_pool._ensure_metrics()` creates `hft_quote_*` lazily) and expand each to the series
   the client library exports (`x_total` + `x_created` for a Counter; `_bucket/_sum/_count` for a Histogram).
4. Classify each token: engine registry, recording rule, external by prefix (`node_`, `ClickHouse`, `loki_`, `process_`,
   `python_`, `scrape_`, `prometheus_`, plus `up`/`ALERTS`), or not exported by the registry.
5. Check `{label=...}` matchers against the label names declared by the metric, and `job=` matchers against
   `scrape_configs` in `config/monitoring/prometheus.yml`.
6. Look for the default-0 hazard (§3, F6): an alert that compares `time()` with an unlabeled Gauge, which a Prometheus client
   exports as `0` from the start.
7. Every alert flagged in step 4 or 6 was then checked by hand with `grep` in source, scripts, compose and docs.

Apart from the alerts named in F5 and F6, the alerts the script did not flag were classified by the script only; none was
individually traced to the line that updates its series. The label check found no unknown label and no unknown `job`.

## 3. Findings

| ID | Alert(s) | Severity | Finding | Evidence |
|---|---|---|---|---|
| F1 | `ExecFillDataLossDetected` | critical, `for: 0s` | **Reads a name that is never exported.** The alert expression is `increase(exec_fill_data_loss[5m]) > 0`. The metric is a Counter named `exec_fill_data_loss_total`, so the exported series is `exec_fill_data_loss_total`. The alert for permanently lost fills cannot fire. | `config/monitoring/alerts/rules.yaml:890-891`; Counter at `src/hft_platform/observability/metrics.py:1739-1740`; incremented at `src/hft_platform/execution/router.py:1086`. The bare `exec_fill_data_loss` at `metrics.py:394` is an entry in a list of prefixes to unregister, not a metric. Two neighbouring alerts (`RecorderExecDropsDetected`, `RecorderExecWalFallbackFailure`, `rules.yaml:869-886`) read correctly named series and still cover the recorder side of the same event. |
| F2 | `ReplayParitySimDegraded`, `ReplayParityShadowDegraded`, `ReplayParityLiveDegraded`, `ReplayParityDailyJobStale`, `ReplayParityPreRecorder` | critical / warning / info | **Never loaded, and the producer writes where nothing reads.** `prometheus.yml` lists only `rules.yaml` under `rule_files`, so `replay_parity_alert.yml` is not evaluated. The series `hft_replay_match_pct` is written by `scripts/ops/daily_replay_diff.py` to `/var/lib/node_exporter/textfile_collector/hft_replay_match.prom` (underscore), while node-exporter reads `/var/lib/node-exporter/textfile` (hyphen). Both would have to change before these alerts mean anything. The same `rule_files` gap also leaves `recording_rules/slo.yaml` unevaluated, so the two `slo:*` series used by two alerts do not exist. | `config/monitoring/prometheus.yml:5-6`; `scripts/ops/daily_replay_diff.py:58-61`; `docker-compose.yml:351,357`. |
| F3 | `ClickHouseSystemLogSizeCritical` | warning, `for: 10m` | **No producer in the repo.** It reads `clickhouse_table_parts_bytes{database,table}`. Nothing in `src/`, `scripts/` or `docker-compose.yml` exports that name. The `clickhouse` scrape job targets `clickhouse:9363`, the server's built-in endpoint enabled by `config/clickhouse_prometheus.xml`, whose series are named `ClickHouse*`. Whether any exporter on the host adds `clickhouse_table_parts_bytes` is NOT VERIFIED. | `rules.yaml:719-722`; `prometheus.yml` (job `clickhouse`); `config/clickhouse_prometheus.xml`. |
| F4 | `ResearchDataTooLarge`, `SSDReallocatedSectorsHigh`, `SSDWearLevelLow` | warning / critical / warning | **Depend on cron scripts and on node-exporter being scraped.** `scripts/research_data_rotate.sh` (`hft_research_data_bytes`) and `scripts/smart_check.sh` (`smartmon_*`) write to `/var/lib/node-exporter/textfile`, the directory the compose service mounts. Both run from cron (`docs/operations/cron-setup-remote.md:97` Sunday 04:00 and `:102` Monday 05:00). If the cron entry is missing or the `node` target is down, the series is absent and the alert stays silent without any other signal. Whether the `node` target is up on the host is NOT VERIFIED (an earlier review recorded it as unreachable there). | `rules.yaml:1386-1411`; `scripts/research_data_rotate.sh:88-90`; `scripts/smart_check.sh:36-40`. |
| F5 | `WALCorruptionDetected` | critical, `for: 0s` | **Series defined, never updated.** `wal_corrupt_files_total` is a registry Counter that no source file increments, so the alert cannot fire. (Classified "registry" by §1; found by the unreferenced-metric scan in §4.) | `rules.yaml:1355-1356`; `metrics.py:1195-1196`. |
| F6 | `BackupStale`, `ContractCacheStale`, `ExecutionRouterHeartbeatStale`, `ExecutionGatewayHeartbeatStale` | critical (`ContractCacheStale`: warning) | **Default-0 gauge hazard, handled by a zero gate in all four.** Each compares `time()` with an unlabeled Gauge that exports `0` until set, so each carries `> 0` on the gauge (otherwise they would fire at once). The gate is also why they stay silent while the gauge is never set. For three of the four, a setter runs in the engine process: the heartbeats at `execution/router.py:467,482`, `execution/gateway.py:34` and `services/system.py:2238-2240`; `contract_cache_last_success_ts` at `feed_adapter/shioaji/session_runtime.py:370`. **`BackupStale` is the exception:** `scripts/clickhouse_backup.sh` runs the backup in a one-shot `docker compose exec` process with its own registry, so the engine exports `hft_backup_last_success_ts = 0` for ever and the `> 0` gate (added 2026-04-25) switches the alert off. A backup that stopped working would not alert. | `rules.yaml:1027` (BackupStale), `:264`, `:767`, `:1345`. |

Findings that need a code or rule change are not fixed here. This document changes no behavior.

## 4. Metrics defined but not updated

The unreferenced-metric scan (`scripts/dead_metrics_scan.py` from a separate, unmerged tooling change; not part of this
document) reports, for `53f8b815`: 276 metrics defined, **7 with no reference in `src/` outside their definition**,
47 reachable only through dynamic attribute access (informational), and one alert on an unreferenced metric (F5).

The 7: `clickhouse_pool_active`, `clickhouse_pool_checkout_latency_ms`, `clickhouse_pool_timeout_total`,
`execution_gateway_errors_total`, `gateway_exposure_notional_scaled`, `portfolio_trade_count`, `wal_corrupt_files_total`.
Only `wal_corrupt_files_total` is read by an alert. The one alert on a `clickhouse_pool_*` metric (`ClickHousePoolExhausted`)
is commented out in `rules.yaml:1373-1377` with the note that the counter is never incremented; the other two have no alert.

"Referenced in source" is weaker than "updated in the engine process on the production path"; the other 86 registry alerts
were not traced that far.

## 5. Not run

| Check | Why not |
|---|---|
| Prometheus `ALERTS` / `/api/v1/rules` on the host | Needs host access, which is read-only and per-operation authorized. This is the ground truth for which alerts have ever been pending or firing, and for the host's copy of `rules.yaml` (`docs/runbooks/deployment.md:26` lists it among the files hand-edited on the host as of that runbook; NOT VERIFIED to match the repo). |
| `promtool check rules` on the full set | Not part of this change; the alert rules test is `config/monitoring/alerts/rules_test.yaml`, run with `promtool test rules`, and is not in CI. |
| Alertmanager routing of the 55 warning and 1 info alerts | Not checked. |
| Alerts on labeled metrics before the first child exists | A labeled metric exports nothing until a child is created, so an alert that needs a value rather than an absence stays silent until then. Not enumerated. |
| A live exporter for `clickhouse_table_parts_bytes` | F3 stays NOT VERIFIED. |

## 6. Reproducing

The classification is a one-off script (steps 1-6 in §2, about 200 lines, `ast` + `yaml` + regular expressions for PromQL
tokens). It is not committed: a durable version belongs with a guard test that fails when an alert reads a series nobody
exports, with an allowlist that names the producer of each external series (the recording rules and textfile scripts above).
Without it, redo steps 1-5 with the tooling of your choice and compare with the appendix.

## Appendix: every alert and the series it reads

Columns: alert, severity, `for`, file (`rules` = `rules.yaml`, `replay` = `replay_parity_alert.yml`), series read.

| Alert | Severity | For | File | Series |
|---|---|---|---|---|
| ReplayParitySimDegraded | warning | 0s | replay | `hft_replay_match_pct` (not in the engine registry) |
| ReplayParityShadowDegraded | warning | 0s | replay | `hft_replay_match_pct` (not in the engine registry) |
| ReplayParityLiveDegraded | critical | 0s | replay | `hft_replay_match_pct` (not in the engine registry) |
| ReplayParityDailyJobStale | warning | 5m | replay | `hft_replay_match_pct` (not in the engine registry) |
| ReplayParityPreRecorder | info | 0s | replay | `hft_replay_match_pct` (not in the engine registry) |
| HFTEngineDown | critical | 30s | rules | `up` (external) |
| FeedGapCritical | critical | 5s | rules | `feed_events_total`, `market_trading_hours_active` |
| FeedReconnectTimeoutDetected | warning | 0s | rules | `feed_reconnect_timeout_total` |
| FeedReconnectExceptionDetected | warning | 0s | rules | `feed_reconnect_exception_total` |
| SymbolConfigReloadFailed | critical | 1m | rules | `feed_symbol_config_reload_total` |
| FeedSubscriptionPermanentlyFailed | warning | 15m | rules | `feed_subscription_permanent_failures_total`, `market_trading_hours_active` |
| FeedTimeSkewCritical | warning | 10m | rules | `feed_time_skew_over_threshold_total`, `market_trading_hours_active` |
| RecorderDeadLetterQueueGrowing | critical | 0s | rules | `dlq_size_total` |
| OrderDeadLetterQueueGrowing | critical | 0s | rules | `dlq_size_total` |
| RecorderDeadLetterQueueNonEmpty | warning | 30m | rules | `wal_file_count_tiered` |
| WALReplayBacklogStalled | critical | 15m | rules | `wal_backlog_files`, `wal_replay_throughput_rows_total` |
| FeedResubscribeFailing | warning | 0s | rules | `feed_resubscribe_total` |
| ContractCacheStale | warning | 30m | rules | `contract_cache_last_success_ts`, `market_trading_hours_active` |
| ContractsStaleVsBrokerAnnouncement | warning | 15m | rules | `contract_update_last_event_ts`, `contract_cache_last_success_ts`, `contract_event_callback_registered` |
| ContractEventCallbackUnregistered | warning | 10m | rules | `contract_event_callback_registered` |
| FeedReconnectFailureRatioHigh | warning | 10m | rules | `feed_reconnect_total`, `market_trading_hours_active` |
| QuoteWatchdogCallbackReregisterHigh | warning | 10m | rules | `quote_watchdog_recovery_attempts_total` |
| ShioajiWatchdogThreadDown | critical | 1m | rules | `shioaji_thread_alive`, `market_trading_hours_active` |
| ShioajiPendingQuoteStall | warning | 5m | rules | `shioaji_quote_pending_age_seconds`, `market_trading_hours_active` |
| QuoteFacadeLoggedOut | warning | 5m | rules | `hft_quote_conn_logged_in`, `market_trading_hours_active` |
| QuotePoolFullyUnsubscribed | critical | 2m | rules | `hft_quote_conn_subscribed_count`, `market_trading_hours_active` |
| QuoteFacadeDataStalled | critical | 5m | rules | `hft_quote_conn_last_data_age_s`, `market_trading_hours_active` |
| QuotePoolMetricsPublisherStalled | critical | 5m | rules | `hft_quote_pool_metrics_published_total`, `market_trading_hours_active` |
| ShioajiSessionRefreshThreadDown | critical | 10m | rules | `shioaji_thread_alive` |
| ShioajiLoginFailuresDetected | warning | 0s | rules | `shioaji_login_fail_total` |
| ShioajiCrashSignatureDetected | critical | 0s | rules | `shioaji_crash_signature_total`, `market_trading_hours_active` |
| FeedSessionConflictDetected | warning | 0s | rules | `feed_session_conflict_total` |
| ShioajiSessionLockConflictDetected | warning | 0s | rules | `shioaji_session_lock_conflicts_total` |
| BusOverflowCritical | critical | 1s | rules | `bus_overflow_total` |
| StormGuardHalt | critical | 0s | rules | `stormguard_mode` |
| StormGuardHaltStuck | critical | 5m | rules | `stormguard_mode` |
| HighRejectRate | warning | 1m | rules | `order_reject_total`, `order_actions_total` |
| ExecutionLagHigh | warning | 30s | rules | `execution_router_lag_ns_bucket` |
| QueueDepthSpike | warning | 30s | rules | `queue_depth` |
| ExecutionGatewayTaskDown | critical | 1m | rules | `execution_gateway_alive`, `stormguard_mode` |
| ExecutionRouterTaskDown | critical | 1m | rules | `execution_router_alive` |
| RawQueueDropsDetected | warning | 0s | rules | `raw_queue_dropped_total` |
| ShioajiCallbackIngressLatencyP99High | warning | 2m | rules | `shioaji_quote_callback_ingress_latency_ns_bucket` |
| CallbackParseMissDetected | warning | 0s | rules | `market_data_callback_parse_total` |
| RecorderFailure | critical | 0s | rules | `recorder_failures_total` |
| RecorderInsertFailedRatioHigh | warning | 15m | rules | `recorder_insert_batches_total` |
| RecorderInsertRetryRatioHigh | warning | 15m | rules | `recorder_insert_batches_total` |
| GatewayDispatchLatencyP99High | warning | 2m | rules | `gateway_dispatch_latency_ns_bucket` |
| GatewayIntentChannelDepthHigh | warning | 1m | rules | `gateway_intent_channel_depth` |
| QuoteSchemaMismatchDetected | critical | 0s | rules | `quote_schema_mismatch_total` |
| FeatureShadowParityMismatchDetected | critical | 2m | rules | `feature_shadow_parity_mismatch_total` |
| FeaturePlaneLatencyP99High | warning | 5m | rules | `feature_plane_latency_ns_bucket` |
| FeatureQualityFlagsSpike | warning | 2m | rules | `feature_quality_flags_total` |
| FeaturePlaneUpdateErrorRatioHigh | warning | 10m | rules | `feature_plane_updates_total` |
| HostDiskSpaceCritical | critical | 1m | rules | `node_filesystem_avail_bytes` (external), `node_filesystem_size_bytes` (external) |
| HostDiskSpaceWarn | warning | 5m | rules | `node_filesystem_avail_bytes` (external), `node_filesystem_size_bytes` (external) |
| ClickHouseSystemLogSizeCritical | warning | 10m | rules | `clickhouse_table_parts_bytes` (not in the engine registry) |
| ReconciliationDiscrepancyDetected | critical | 1m | rules | `reconciliation_discrepancy_count` |
| ReconciliationSamplesUntrustworthy | critical | 1m | rules | `reconciliation_untrusted_sample_streak` |
| ExecutionRouterHeartbeatStale | critical | 0s | rules | `execution_router_heartbeat_ts` |
| ExecutionRouterLagP99High | warning | 1m | rules | `execution_router_lag_ns_bucket` |
| WALReplayLagHigh | warning | 2m | rules | `wal_replay_lag_seconds` |
| WALBacklogFilesHigh | warning | 2m | rules | `wal_backlog_files` |
| WALDrainETAHigh | warning | 2m | rules | `wal_drain_eta_seconds` |
| ClickHouseConnectionDown | critical | 2m | rules | `clickhouse_connection_health` |
| WALDiskPressureCritical | critical | 30s | rules | `disk_pressure_level` |
| WALDiskCircuitBreakerActive | critical | 1m | rules | `wal_disk_circuit_breaker_active` |
| PipelineDataLoss | critical | 0s | rules | `pipeline_health_state` |
| RecorderReinjectCircuitBreakerOpen | critical | 0s | rules | `recorder_reinject_circuit_breaker_drops_total` |
| RecorderExecDropsDetected | critical | 0s | rules | `recorder_exec_drops_total` |
| RecorderExecWalFallbackFailure | critical | 0s | rules | `recorder_exec_wal_fallback_failure_total` |
| ExecFillDataLossDetected | critical | 0s | rules | `exec_fill_data_loss` (not in the engine registry) |
| RecorderSchemaInitFailed | critical | 0s | rules | `recorder_schema_init_failed` |
| EventLoopLagHigh | warning | 2m | rules | `event_loop_probe_lag_ms_bucket`, `market_trading_hours_active` |
| StrategyExceptionsRateHigh | warning | 2m | rules | `strategy_exceptions_total` |
| PortfolioDrawdownHigh | warning | 1m | rules | `portfolio_drawdown_pct` |
| PortfolioDrawdownCritical | critical | 0s | rules | `portfolio_drawdown_pct` |
| OrphanedFillDetected | warning | 5m | rules | `orphaned_fill_total` |
| SLOFeedAvailabilityBurnRateHigh | warning | 5m | rules | `slo:feed_availability:ratio_rate5m` (recording rule) |
| SLOOrderSuccessRateLow | warning | 5m | rules | `slo:order_success:ratio_rate5m` (recording rule) |
| LokiDown | warning | 2m | rules | `up` (external) |
| BackupStale | critical | 1h | rules | `hft_backup_last_success_ts` |
| FillEvictionDetected | critical | 0s | rules | `exec_overflow_evicted_total` |
| AlphaSignalSilent | warning | 1m | rules | `alpha_signal_events_total`, `alpha_last_signal_ts`, `market_trading_hours_active` |
| StrategyBoundToNoLiveSymbols | critical | 5m | rules | `strategy_bound_live_symbols`, `market_trading_hours_active` |
| StrategyNotProcessingEvents | warning | 10m | rules | `strategy_events_received_total`, `market_trading_hours_active` |
| StrategyHoldsPositionButEmitsNoIntents | warning | 15m | rules | `position_age_seconds`, `strategy_intents_total`, `market_trading_hours_active` |
| StrategyPendingWithNoInflightOrder | warning | 15m | rules | `strategy_pending_qty`, `strategy_inflight_oids`, `market_trading_hours_active` |
| StrategyTimeoutCircuitBroken | warning | 0s | rules | `strategy_circuit_break_total` |
| StrategyHandlerTimingOut | warning | 5m | rules | `strategy_timeout_total` |
| FeedConnectGateBlockedConnect | critical | 0s | rules | `feed_connect_gate_blocked_total` |
| StrategyQuarantineActive | critical | 0s | rules | `strategy_quarantine_active` |
| PlatformReduceOnlyActive | critical | 0s | rules | `platform_reduce_only_active` |
| ManualRearmRequired | critical | 0s | rules | `manual_rearm_required` |
| StrategyQuarantineUndurable | critical | 30s | rules | `strategy_quarantine_undurable` |
| StrategyQuarantinePersistFailing | critical | 0s | rules | `strategy_quarantine_persist_failed_total` |
| AutonomyStateTransition | warning | 0s | rules | `autonomy_transitions_total` |
| ExecutionGatewayHeartbeatStale | critical | 30s | rules | `execution_gateway_heartbeat_ts`, `stormguard_mode` |
| WALCorruptionDetected | critical | 0s | rules | `wal_corrupt_files_total` |
| WALReplayErrorsDetected | warning | 0s | rules | `wal_replay_errors_total` |
| ResearchDataTooLarge | warning | 1h | rules | `hft_research_data_bytes` (not in the engine registry) |
| SSDReallocatedSectorsHigh | critical | 0s | rules | `smartmon_reallocated_sectors` (not in the engine registry) |
| SSDWearLevelLow | warning | 0s | rules | `smartmon_wear_leveling` (not in the engine registry) |
