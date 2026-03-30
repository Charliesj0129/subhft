# Orphaned Metrics Triage (2026-03-30)

## Summary

- Total exported: 145
- In alerts: 54
- In dashboards: 41
- In recording rules: 7
- Covered (union of alerts ∪ dashboards ∪ recording rules): 74
- **Orphaned: 71**

Triage buckets:
- **(a) Wire to dashboard** — operationally useful; should be added to existing or new dashboards
- **(b) Wire to alert** — should trigger an alert condition
- **(c) Intentionally unmonitored** — debug/internal counters, companions to already-alerted metrics, or metrics consumed by manual investigation rather than automated monitoring

## Observation: Phantom Dashboard Reference

`recorder_insert_latency_ms` is referenced in dashboard expressions (bucket notation) but does **not** exist in `metrics.py`. This is a dead panel. The intended metric is likely `recorder_wal_write_latency_ms` or `clickhouse_pool_checkout_latency_ms`. Should be fixed in the next dashboard pass.

---

## Triage

### Bucket (b) — Wire to Alert

| Metric | Type | Notes |
|--------|------|-------|
| `exec_queue_overflow_total` | Counter | Fills routed to overflow buffer when raw_exec_queue is full; precursor to fill loss. Alert if increasing. |
| `execution_router_errors_total` | Counter | Execution router errors — any increase is operational concern. |
| `execution_gateway_errors_total` | Counter | Execution gateway errors — same as above. |
| `intent_queue_full_total` | Counter | Intents dropped in StrategyRunner submit loop; indicates hot-path backpressure. |
| `order_queue_full_total` | Counter | Approved commands dropped due to order_queue full in RiskEngine; commands are silently lost. |
| `risk_halt_blocked_total` | Counter | Commands blocked by RiskEngine HALT guard; useful for confirming HALT enforcement is working. |
| `deferred_terminal_expired_total` | Counter | Deferred terminal callbacks that expired without resolution — indicates fill processing failures. |
| `terminal_before_registration_total` | Counter | Terminal callbacks arriving before order registered — race condition indicator. |
| `dlq_size_total` | Counter | Dead Letter Queue entries; any DLQ growth deserves an alert. |
| `shioaji_keepalive_failures_total` | Counter | Keep-alive failures indicate session health degradation before reconnect events. |
| `shioaji_contract_lookup_errors_total` | Counter | Contract lookup failures by symbol; missing contracts cause silent order failures. |
| `shioaji_quote_callback_queue_dropped_total` | Counter | Drops in the callback ingress queue — data loss upstream of the normalizer. |
| `reconciliation_consecutive_failures` | Gauge | Consecutive reconciliation failures; should alert at ≥3 to avoid silent position drift. |
| `reconciliation_last_success_ts` | Gauge | Staleness guard: `time() - reconciliation_last_success_ts > threshold` mirrors `execution_router_heartbeat_ts` pattern. |
| `feature_profile_compat_failures_total` | Counter | Strategy/feature compatibility failures block feature adoption silently. |

### Bucket (a) — Wire to Dashboard

| Metric | Type | Notes |
|--------|------|-------|
| `shioaji_api_latency_ms` | Histogram | Broker API round-trip latency by operation. Core latency panel for the execution plane. |
| `shioaji_api_errors_total` | Counter | API errors by operation; complements latency histogram. |
| `shioaji_api_jitter_ms` | Gauge | API jitter gauge (current value). |
| `shioaji_api_jitter_ms_hist` | Histogram | API jitter distribution; useful for tail latency analysis. |
| `feed_interarrival_ns` | Histogram | Feed inter-arrival time distribution; useful for detecting feed thinning or gaps. |
| `feed_gap_by_symbol_seconds` | Gauge | Per-symbol feed gap; should be a heatmap row in the feed-health dashboard. |
| `feed_time_skew_ns` | Gauge | Exchange vs. local timestamp skew by topic; clock drift indicator. |
| `feed_last_event_ts` | Gauge | Last feed event timestamp by source; freshness indicator. |
| `clickhouse_pool_active` | Gauge | Active ClickHouse connection pool connections. Add to gateway_wal_slo dashboard. |
| `clickhouse_pool_checkout_latency_ms` | Histogram | Pool checkout latency; pairs with pool timeout alert. |
| `recorder_wal_write_latency_ms` | Histogram | WAL write latency by writer/mode; important for recorder durability SLO. |
| `recorder_wal_fsync_latency_ms` | Histogram | WAL fsync latency; critical for WAL durability guarantee. |
| `recorder_rows_flushed_total` | Counter | Rows flushed per table; throughput panel. |
| `wal_directory_size_bytes` | Gauge | WAL directory size; should be visible in the WAL SLO dashboard. |
| `wal_file_count` | Gauge | Pending WAL file count; leading indicator before `wal_backlog_files` alert fires. |
| `wal_disk_available_mb` | Gauge | Available disk for WAL; operational awareness panel. |
| `wal_oldest_file_age_seconds` | Gauge | Age of oldest WAL file; complements `wal_replay_lag_seconds`. |
| `portfolio_total_pnl` | Gauge | Total realized PnL across all positions; primary P&L dashboard panel. |
| `portfolio_trade_count` | Counter | Total trade count by strategy/side; activity gauge. |
| `gateway_exposure_notional_scaled` | Gauge | Per-strategy/symbol exposure; critical for position sizing visibility. |
| `gateway_policy_mode` | Gauge | Current gateway policy mode (NORMAL/DEGRADE/HALT); state panel. |
| `shadow_mode_active` | Gauge | Shadow order mode status; should be visible to confirm live/shadow state. |
| `shadow_orders_total` | Counter | Shadow orders intercepted; throughput metric for shadow validation runs. |
| `strategy_position` | Gauge | Net position per strategy/symbol; primary position panel. |
| `hft_backup_size_bytes` | Gauge | Backup size; should appear alongside `hft_backup_last_success_ts` in ops dashboard. |
| `hft_backup_duration_seconds` | Gauge | Backup duration; latency indicator for backup jobs. |
| `hft_backup_retained_count` | Gauge | Backups currently retained; confirms retention policy is working. |
| `reconciliation_sync_total` | Counter | Reconciliation sync outcomes by result; success/failure rates. |
| `reconciliation_sync_duration_seconds` | Histogram | Reconciliation sync duration; latency panel. |
| `lob_updates_total` | Counter | LOB updates applied by symbol/type; market data throughput. |

### Bucket (c) — Intentionally Unmonitored

| Metric | Type | Notes |
|--------|------|-------|
| `latency_spans_dropped_total` | Counter | Internal observability buffer overflow counter; debug only. |
| `lob_snapshots_total` | Counter | LOB snapshot count by symbol; internal pipeline counter, low operational value. |
| `feed_first_quote_total` | Counter | First live quote at startup; fires once per session, not useful for ongoing monitoring. |
| `feed_resubscribe_total` | Counter | Feed resubscribe attempts; low-level companion to `feed_reconnect_total` which is already alerted. |
| `feed_session_lease_ops_total` | Counter | Redis feed session lease ops by op/result; internal session management detail. |
| `alpha_signal_events_total` | Counter | Alpha signal decisions by outcome; `alpha_last_signal_ts` is already alerted for signal silence. |
| `strategy_skew` | Gauge | Price skew adjustment per strategy/symbol; internal MM tuning param, not ops-critical. |
| `strategy_micro_price` | Gauge | Computed micro-price; internal signal value, research/debug use. |
| `feature_profile_rollout_state` | Gauge | Feature profile rollout state; low-frequency config state, not ops-critical. |
| `feature_profile_activations_total` | Counter | Feature profile activate/rollback events; infrequent lifecycle events. |
| `wal_mode` | Gauge | Current recorder WAL mode (0=direct, 1=wal_first); set at startup, not dynamic. |
| `market_open_grace_active` | Gauge | Market open grace period active; transient state, low monitoring value. |
| `quote_version_switch_total` | Counter | Quote version upgrade/downgrade events; infrequent, visible in logs. |
| `session_refresh_total` | Counter | Preventive session refresh results; internal lifecycle event. |
| `contract_refresh_total` | Counter | Contract refresh operations; infrequent, low ops value. |
| `contract_refresh_symbols_changed_total` | Counter | Symbol changes after contract refresh; informational, not actionable. |
| `wal_batch_flush_total` | Counter | WAL batch flush at market close; lifecycle event, low ongoing value. |
| `wal_batch_flush_retry_total` | Counter | WAL batch flush retries; companion to `wal_batch_flush_total`, debug only. |
| `recorder_insert_retry_total` | Counter | Recorder insert retries per table; `recorder_insert_batches_total` with `result` label covers this in alerts. |
| `recorder_wal_skipped_rows_total` | Counter | WAL rows skipped due to disk pressure; companion to `disk_pressure_level` which is already alerted. |
| `wal_disk_circuit_breaker_active` | Gauge | WAL disk circuit breaker state per writer; `disk_pressure_level` alert covers the actionable condition. |
| `shioaji_quote_route_total` | Counter | Quote callback route outcomes (miss/fallback/drop); debug-level routing detail. |
| `shioaji_quote_pending_stall_total` | Counter | Pending quote-resubscribe stall events; `shioaji_quote_pending_age_seconds` alert covers this. |
| `shioaji_quote_callback_queue_depth` | Gauge | Callback ingress queue depth; `shioaji_quote_callback_queue_dropped_total` (bucket b) is the actionable signal. |
| `reconciliation_discrepancy_total` | Counter | Discrepancies by severity; `reconciliation_discrepancy_count` gauge is already alerted. |
| `exec_overflow_drained_total` | Counter | Fills successfully drained from overflow; companion to `exec_overflow_evicted_total` which is alerted. |

---

## Coverage Summary by Domain

| Domain | Exported | Covered | Orphaned |
|--------|----------|---------|---------|
| Market Data / Feed | 17 | 11 | 6 |
| Shioaji Adapter | 15 | 8 | 7 |
| Strategy / Risk | 15 | 10 | 5 |
| Execution / Orders | 13 | 9 | 4 |
| Recorder / WAL | 19 | 8 | 11 |
| CE-M2 Gateway | 6 | 4 | 2 |
| Feature Plane | 9 | 5 | 4 |
| Portfolio / PnL | 4 | 1 | 3 |
| Reconciliation | 7 | 1 | 6 |
| Backup | 4 | 1 | 3 |
| System / Infra | 11 | 7 | 4 |
| Pipeline Determinism | 8 | 1 | 7 |
| Contract Refresh | 2 | 0 | 2 |
| Misc | 1 | 1 | 0 |
