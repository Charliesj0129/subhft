# observability

## Purpose

Prometheus metrics, latency recording, and runtime instrumentation.

## Key Files

| File                       | Key Class         | Purpose                                  |
| -------------------------- | ----------------- | ---------------------------------------- |
| `observability/metrics.py` | `MetricsRegistry` | Singleton holding all Prometheus metrics |
| `observability/latency.py` | `LatencyRecorder` | Stage-based latency tracking for tracing |

## MetricsRegistry (Key Metrics)

| Metric                        | Type      | Labels                             | Purpose                          |
| ----------------------------- | --------- | ---------------------------------- | -------------------------------- |
| `feed_last_event_ts`          | Gauge     | `source`                           | Last tick timestamp for liveness |
| `raw_queue_depth`             | Gauge     | —                                  | raw_queue backpressure           |
| `raw_queue_dropped_total`     | Counter   | —                                  | Dropped ticks (queue full)       |
| `feed_reconnect_total`        | Counter   | `result`                           | Reconnect attempts               |
| `strategy_latency_ns`         | Histogram | `strategy`                         | Per-strategy compute latency     |
| `strategy_intents_total`      | Counter   | `strategy`                         | OrderIntent emit count           |
| `strategy_exceptions_total`   | Counter   | `strategy, exception_type, method` | Strategy errors                  |
| `gateway_dispatch_latency_ns` | Histogram | —                                  | Gateway pipeline latency         |
| `gateway_reject_total`        | Counter   | `reason`                           | Gateway rejections               |
| `recorder_failures_total`     | Counter   | —                                  | Recording failures               |
| `wal_mode`                    | Gauge     | —                                  | 1=wal_first, 0=direct            |

## LatencyRecorder

Records per-event latency with trace_id for end-to-end tracing:

```python
self.latency.record("normalize", duration_ns, trace_id=trace_id, symbol=symbol)
```

Stages: `normalize`, `lob_process`, `strategy`, `risk`, `order`.

## Configuration

| Variable                            | Default          | Purpose                                                          |
| ----------------------------------- | ---------------- | ---------------------------------------------------------------- |
| `HFT_PROM_PORT`                     | `9090`           | Prometheus scrape port                                           |
| `HFT_OBS_POLICY`                    | `balanced`       | `minimal` / `balanced` / `debug` — controls metric sampling rate |
| `HFT_STRATEGY_METRICS_SAMPLE_EVERY` | varies by policy | How often to observe strategy latency                            |
| `HFT_MD_METRICS_SAMPLE_EVERY`       | varies by policy | How often to observe MD metrics                                  |

## OBS Policy

| Policy     | Strategy Sample | MD Sample  | Callback Parse Sample |
| ---------- | --------------- | ---------- | --------------------- |
| `minimal`  | every 8th       | every 16th | every 64th            |
| `balanced` | every 2nd       | every 4th  | every 64th            |
| `debug`    | every 1         | every 1    | every 1               |

## Gotchas

- Metrics use **deferred imports** in gateway/risk to avoid circular imports. Never move to top-level.
- `MetricsRegistry.get()` is a singleton. It returns `None` if Prometheus is not started.
- Required minimum metrics are documented in `docs/observability_minimal.md`.
