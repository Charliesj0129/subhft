# Observability Minimal Set

本文件定義最小可觀測性集合與基本告警。

## 1) Feed / Ingest
- `feed_events_total{type=...}`
- `feed_latency_ns` (histogram)
- `feed_interarrival_ns` (histogram)
- `feed_last_event_ts{source=...}`
- `feed_reconnect_total{result=...}`
- `feed_resubscribe_total{result=...}`
- `normalization_errors_total{type=...}`

## 2) Pipeline / Queue
- `queue_depth{queue=...}`
- `bus_overflow_total`
- `event_loop_lag_ms`

## 3) Strategy / Risk
- `strategy_latency_ns`
- `strategy_intents_total{strategy=...}`
- `risk_reject_total{reason=...,strategy=...}`
- `stormguard_mode{strategy=...}`

## 4) Execution / Order
- `order_actions_total{type=...}`
- `order_reject_total`
- `execution_events_total{type=...}`
- `execution_router_lag_ns`

## 5) Recorder / ClickHouse
- `recorder_failures_total`
- `recorder_batches_flushed_total{table=...}`
- `recorder_rows_flushed_total{table=...}`
- `recorder_wal_writes_total{table=...}`

## 6) Shioaji API
- `shioaji_api_latency_ms{op=...,result=...}`
- `shioaji_api_jitter_ms{op=...}`
- `shioaji_api_errors_total{op=...}`

## 7) Baseline Alerts
- Feed Gap: `time() - feed_last_event_ts > 15s`（持續 1m）
- Latency P99 異常
- Queue backlog 持續上升
- `increase(recorder_failures_total[5m]) > 0`
- `event_loop_lag_ms` 長時間超標

## 8) 最小健康檢查流程
```bash
# 1) metrics 可抓
curl -fsS http://localhost:9090/metrics >/tmp/metrics.txt

# 2) 不應出現 traceback
rg -n "Traceback|AttributeError|Catcher" /tmp/metrics.txt

# 3) 核心 metric 存在
rg -n "feed_events_total|queue_depth|shioaji_api_latency_ms" /tmp/metrics.txt
```
