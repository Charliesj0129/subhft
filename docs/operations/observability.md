# Observability Minimal Set

本文件定義最小可觀測性集合與基本告警。

## 1) Feed / Ingest
- `feed_events_total{type=...}`
- `feed_latency_ns` (histogram)
- `feed_interarrival_ns` (histogram)
- `feed_last_event_ts{source=...}`
- `feed_reconnect_total{result=...}`
- `feed_first_quote_total`
- `quote_watchdog_recovery_attempts_total{action=...}`
- `feed_resubscribe_total{result=...}` — the `result` label carries two
  different kinds of value. `event_4` / `event_13` are **triggers** (the SDK
  asked for a resubscribe); `ok` / `error` / `skip` are **outcomes** of an
  attempt. Triggers greatly outnumber outcomes because
  `QuoteRuntime.schedule_resubscribe` coalesces while one is already armed,
  so a large `event_4` count against a small `ok` count is normal. Alert on
  `result="error"`, never on the trigger labels.
- `normalization_errors_total{type=...}`

## 2) Pipeline / Queue
- `queue_depth{queue=...}`
- `bus_overflow_total`
- Event-loop metrics (three, and they are not interchangeable):
  - `event_loop_probe_lag_ms` (Histogram): overshoot of an idle probe task that only sleeps
    (`HFT_LOOP_PROBE_INTERVAL_S`, default 0.1 s). This is the real loop congestion: time the loop was
    unavailable to any callback, hot path included. `EventLoopLagHigh` reads the histogram. StormGuard's loop-lag input is the probe's peak since the previous supervisor tick (`_stormguard_latency_us`); for a tick in which the probe recorded no sample it falls back to `event_loop_lag_ms`.
  - `supervisor_tick_duration_ms` (Histogram): wall time of one supervisor tick body, excluding its 1 Hz sleep.
    The body contains `await`s, so this is elapsed time, not time the loop was blocked.
  - `event_loop_lag_ms` (Gauge): the supervisor's whole tick period minus its 1 s sleep, so it is congestion
    **plus** the tick body, roughly the sum of the other two. Despite the name it is not loop lag; the name is
    kept because a dashboard (`pipeline-health.json`) still plots it. Do not alert or tune on it.
  - The two Histograms expose `_bucket`, `_sum` and `_count` only; a query for the bare name returns nothing.
  - `execution_router_lag_ns` (`ExecutionLagHigh`) is time from an execution report's broker-thread stamp to the
    router dequeuing it, so it rises with loop congestion, not with broker latency. The market-data consumer
    yields to the loop every `HFT_MD_YIELD_EVERY` consecutive queued ticks (default 1); without that yield a tick
    burst was drained in one loop step and blocked execution reports for 25-50 ms (THESHOW day session, 2026-10-21).
    The default was 8 until 2026-11: at ~600 us of CPU per feed event an 8-message step still produced 400-1,300
    probe overshoots >10 ms per hour in the day session, scaling with tick volume (ExecutionLagHigh after the
    10/28 deploy). A yield costs ~2.4 us.

## 3) Strategy / Risk
- `strategy_latency_ns`
- `strategy_intents_total{strategy=...}`
- `risk_reject_total{reason=...,strategy=...}`
- `stormguard_mode{strategy=...}`
- `stormguard_halt_reason{reason=...}` (1 on the cause the current HALT was entered for: the `trigger_halt` reason up to its first `:`; 0 once HALT is left)
- `kill_switch_active` (1 while the kill-switch hold is set: latch file present, or a startup-recovery halt pinned it); alert `KillSwitchActive`
- `manual_latch_active{latch=...}` (1 while a durable manual latch file is present); alert `ManualLatchActive`
- `kill_switch_flatten_blocked` (1 while the kill switch holds open positions it will not close: no operator set it, or no recent reconciliation confirmed them); alert `KillSwitchFlattenBlocked`
- `gateway_reject_total{reason=...}` when `HFT_GATEWAY_ENABLED=1`
- `platform_reduce_only_active`
- `manual_rearm_required{scope=...}`
- `autonomy_transitions_total{scope=...,from_mode=...,to_mode=...,reason=...}`

## 4) Execution / Order
- `order_actions_total{type=...}`
- `order_reject_total`
- `shadow_orders_total{strategy=...,symbol=...,side=...}`
- `shadow_mode_active`
- `execution_events_total{type=...}`
- `execution_router_lag_ns`

## 5) Recorder / ClickHouse
- `recorder_failures_total`
- `recorder_insert_batches_total{table=...,result=...}`
- `recorder_batches_flushed_total{table=...}`
- `recorder_rows_flushed_total{table=...}`
- `recorder_wal_writes_total{table=...}`

## 6) Shioaji API
- `shioaji_api_latency_ms{op=...,result=...}`
- `shioaji_api_jitter_ms{op=...}`
- `shioaji_api_errors_total{op=...}`

## 7) Baseline Alerts
- Feed Gap: `time() - feed_last_event_ts > 15s`（持續 1m）
- Reconnect 失敗比例：`(fail+exception)/(ok+fail+exception) > 0.2`（持續 10m）
- Quote watchdog callback re-register：`increase(quote_watchdog_recovery_attempts_total{action="callback_reregister"}[1h]) > 10`（持續 10m）
- Latency P99 異常
- Queue backlog 持續上升
- `increase(recorder_failures_total[5m]) > 0`
- Insert failed ratio（24h）：`failed_after_retry|failed_no_client / total > 0.005`（持續 15m）
- Insert retry ratio（24h）：`success_after_retry|failed_after_retry / total > 0.05`（持續 15m）
- `EventLoopLagHigh`：`event_loop_probe_lag_ms` 的 p99 在 5 分鐘內超過 100 ms，持續 2 分鐘（僅盤中）。
  不要改看 `event_loop_lag_ms`，它含 supervisor 自己的工作量。

## 8) 最小健康檢查流程
```bash
# 1) metrics 可抓
curl -fsS http://localhost:9090/metrics >/tmp/metrics.txt

# 2) 不應出現 traceback
rg -n "Traceback|AttributeError|Catcher" /tmp/metrics.txt

# 3) 核心 metric 存在
rg -n "feed_events_total|queue_depth|shioaji_api_latency_ms" /tmp/metrics.txt
```

Shadow / gateway runtime quick checks:
```bash
# Strategy emits intents
grep '^strategy_intents_total' /tmp/metrics.txt

# Shadow intercept is active
grep -E '^(shadow_orders_total|shadow_mode_active)' /tmp/metrics.txt

# Gateway is not silently rejecting futures prices
grep '^gateway_reject_total' /tmp/metrics.txt

# Platform is not stuck in reduce-only
grep -E '^(platform_reduce_only_active|manual_rearm_required|autonomy_transitions_total)' /tmp/metrics.txt
```

Read this carefully:
- `strategy_intents_total > 0` with `shadow_orders_total = 0` means the strategy is alive, but the shadow intercept/write path is not being hit.
- `gateway_reject_total{reason="PRICE_EXCEEDS_CAP: ... > 50000000"}` means price-cap configuration is incompatible with the instrument scale.
- `platform_reduce_only_active 1` means opening orders are intentionally blocked until operator review and re-arm.
