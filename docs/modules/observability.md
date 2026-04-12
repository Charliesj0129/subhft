# observability — Prometheus Metrics & Health

> **Package**: `src/hft_platform/observability/`
> **Runtime Plane**: Observability & Safety

## Overview

Prometheus metrics registry (200+ metrics), HTTP health server, pipeline latency tracing, and system resource polling.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `metrics.py` | `MetricsRegistry` | Singleton registry for 200+ Prometheus metrics |
| `health.py` | `HealthServer`, `DegradationTracker` | HTTP health endpoints (liveness, readiness, status) |
| `latency.py` | `LatencyRecorder` | Pipeline latency spans with sampling |
| `_system_poller.py` | `SystemPoller` | Background CPU/memory metrics via psutil |
| `_label_sanitize.py` | `sanitize_exception_type()` | Exception label cardinality capping |

## MetricsRegistry

```python
metrics = MetricsRegistry.get()  # Thread-safe singleton
metrics.cap_symbol(symbol)       # Cap label cardinality (max 200 → "_other")
metrics.update_system_metrics()  # CPU + memory gauges
```

### Metric Categories (200+ total)

| Category | Examples |
|----------|---------|
| Market Data | `feed_events_total`, `feed_latency_ns`, `normalization_errors_total` |
| Strategy | `strategy_latency_ns`, `strategy_intents_total`, `strategy_exceptions_total` |
| Risk | `risk_reject_total`, `stormguard_mode`, `stormguard_transitions_total` |
| Order | `order_actions_total`, `order_reject_total`, `shadow_orders_total` |
| Execution | `fills_total`, `duplicate_fill_total`, `e2e_order_latency_ns` |
| Recording | `recorder_batches_flushed_total`, `recorder_ch_insert_latency_ms` |
| Reconciliation | `reconciliation_sync_total`, `position_drift_qty` |
| Pipeline | `pipeline_health_state`, `queue_depth`, `event_loop_lag_ms` |
| System | `system_cpu_usage`, `system_memory_usage` |

## HealthServer

Raw asyncio HTTP/1.1 server (no third-party framework):

| Endpoint | Purpose | Response |
|----------|---------|----------|
| `GET /healthz` | Liveness probe | Always 200 |
| `GET /readyz` | Readiness probe | 200 (ready) / 503 (degraded/unavailable) |
| `GET /status` | Full status dump | JSON with queues, degradation, uptime |

### Readiness Checks (9)

1. System running flag
2. Broker login (MD + order)
3. StormGuard state
4. Critical tasks alive (md, strat, order, recorder, risk/gateway)
5. Optional tasks (exec_router, exec_gateway)
6. Feed connected
7. ClickHouse write health
8. Queue pressure (80% threshold)
9. Order path (task alive AND broker connected)

## LatencyRecorder

```python
recorder = LatencyRecorder.get()
recorder.record(stage="strategy", latency_ns=1500, symbol="TXFD6")
```

Valid stages: `normalize`, `lob`, `feature`, `strategy`, `risk`, `order`, `execution`, `gateway`, `record`, `bus_publish`

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_HEALTH_PORT` | `8080` | Health server port |
| `HFT_METRICS_PREFIX` | — | Prefix for all metric names |
| `HFT_METRICS_MAX_LABEL_SYMBOLS` | `200` | Max symbol labels before capping |
| `HFT_LATENCY_TRACE` | `0` | Enable latency span tracing |
| `HFT_LATENCY_METRICS` | `1` | Enable latency metric recording |
| `HFT_OBS_POLICY` | — | `minimal`, `balanced`, or `debug` |
