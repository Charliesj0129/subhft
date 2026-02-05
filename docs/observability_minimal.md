# Observability Minimal Set

以下是「最低要求」的 metrics 與告警建議，對應 `src/hft_platform/observability/metrics.py`。

---

## 1) Feed / Ingest
- `feed_events_total{type=...}`
- `feed_latency_ns` (histogram)
- `feed_interarrival_ns` (histogram)
- `feed_last_event_ts{source=...}`
- `feed_reconnect_total{result=...}`
- `feed_resubscribe_total{result=...}`
- `normalization_errors_total{type=...}`

## 2) Pipeline / Queue
- `queue_depth{queue=raw|rec|risk|order|...}`
- `bus_overflow_total`
- `event_loop_lag_ms`

## 3) Strategy / Risk
- `strategy_latency_ns` (histogram)
- `strategy_intents_total{strategy=...}`
- `risk_reject_total{reason=...,strategy=...}`
- `stormguard_mode{strategy=...}`
- `strategy_position{strategy=...,symbol=...}`

## 4) Execution / Order
- `order_actions_total{type=...}`
- `order_reject_total`
- `execution_events_total{type=...}`
- `execution_router_lag_ns` (histogram)
- `execution_router_alive`
- `execution_gateway_alive`
- `execution_router_heartbeat_ts`
- `execution_gateway_heartbeat_ts`

## 5) Recorder / ClickHouse
- `recorder_failures_total`
- `recorder_batches_flushed_total{table=...}`
- `recorder_rows_flushed_total{table=...}`
- `recorder_wal_writes_total{table=...}`

## 6) Shioaji API
- `shioaji_api_latency_ms{op=...}` (histogram)
- `shioaji_api_jitter_ms{op=...}` (gauge)
- `shioaji_api_errors_total{op=...}`

## 7) System (optional)
- `system_cpu_usage`
- `system_memory_usage`

---

## Alerts (Baseline)

### Feed Gap
- Trigger if `time() - feed_last_event_ts > 15s` for 1m

### Latency P99
- Trigger if `histogram_quantile(0.99, rate(feed_latency_ns_bucket[5m]))` above threshold for 5m

### Queue Backlog
- Trigger if `queue_depth` exceeds configured size for > 1m

### Recorder Failure
- Trigger if `increase(recorder_failures_total[5m]) > 0`

### Event Loop Stall
- Trigger if `event_loop_lag_ms` > 20ms for > 1m

---

## Notes
- 確保 `hft-engine` 有啟動 Prometheus server（預設 :9090）。
- Docker Compose 內 Prometheus 會 scrape `hft-engine`。
- 若 metrics 成本過高，可用 `HFT_METRICS_ENABLED=0` 或提高 `HFT_METRICS_BATCH`。
