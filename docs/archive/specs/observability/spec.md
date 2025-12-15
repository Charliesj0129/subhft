# Observability & Audit Logging

## Problem Statement
Provide comprehensive visibility across the HFT platform’s hot paths (market data, strategy/risk/order, execution/positions) and minimal cold-path/infra components so operators can detect anomalies within seconds, satisfy audit needs, and support future scaling. Observability must include structured logging, metrics, (optional) traces, dashboards, and alerts that cover Shioaji connectivity, event-bus health, risk guardrails, order flow, execution latency, PositionStore accuracy, Async Recorder success, and ClickHouse availability.

## Scope
- **Hot paths**: Market data adapter, normalizer, LOB, event bus; StrategyRunner, Risk & StormGuard, OrderAdapter; Execution normalizer, PositionStore.
- **Infra / cold path**:
  - **v1**: Async Recorder success/failure, ClickHouse reachability, engine process liveness, restart counters.
  - **v2**: Deep ClickHouse internals (insert latency, merge backlog, disk usage, query latency) plus host/system metrics (CPU, memory, NIC, disk IO, temperature) via node exporter or lightweight psutil agent.
- **Audit logging**: Append-only record of order intents/decisions, risk outcomes, StormGuard transitions, connectivity events, config changes.
- **Dashboards & alerts**: Feed health, strategy/order health, PnL & exposure, plus critical/warn alerting via Slack/pager/email; v2 adds dedicated ClickHouse/system dashboards.
- **Telemetry stack**: structlog JSON logs, Prometheus metrics, future optional OpenTelemetry traces, log shipping to ClickHouse/ELK/Loki.

## Out of Scope
- Full system metrics (CPU/mem/NIC) beyond simple health checks (planned for v2).
- Detailed ClickHouse performance monitoring (v2).
- Visualization frontends (Grafana assumed existing).

## Business Value & Goals
1. Detect feed gaps, event-bus backpressure, or risk halts in seconds and alert operators before trading impact escalates.
2. Maintain immutable auditing of all order/risk/StormGuard events for ≥1–3 years (stretch: 5–7) to support compliance and post-mortems.
3. Measure latency budgets (tick-to-event-bus, strategy runtime, order-ack) to ensure SLA compliance (<20 µs feed steps, ≤1 ms tick→order, ≤10 ms execution -> risk).
4. Provide dashboards summarizing system health (feed, strategy/order, PnL/exposure) and key metrics (rate limits, mode transitions).
5. Enable future expansion (OpenTelemetry traces, deeper infra metrics) without redesign.

## Requirements
### Logging / Audit
- Use `structlog` to emit JSON logs with standardized fields: `ts`, `level`, `service`, `component`, `event`, `env`, `symbol`, `strategy_id`, `intent_id`, `client_order_id`, `broker_order_id`, `risk_mode`, `reason_code`, `latency_us`, etc.
- Log categories:
  - **Order lifecycle**: NEW/AMEND/CANCEL intents (with context), broker ACK/REJECT responses, order adapter errors.
  - **Risk decisions**: Accept/reject with reason, StormGuard mode, relevant thresholds.
  - **StormGuard transitions**: old/new mode, trigger metric, values (PnL, rate, etc.).
  - **Connectivity**: login/logout, websocket connect/disconnect, reconnect attempts.
  - **Config changes**: symbol lists, risk limits, strategy toggles; include config hash/version.
- Immutability: ingest logs into ClickHouse tables (e.g., `orders_log`, `risk_log`, `guardrail_log`) or ELK. Append-only, with retention ≥1–3 years (target 5+ for compliance-level).

### Metrics
- Expose Prometheus metrics via `/metrics` HTTP endpoint.
- Categories:
  - **Market data**:
    - `feed_events_total{type}`.
    - `feed_events_per_sec` (derived).
    - `feed_gap_seconds`.
    - `feed_latency_ns` histogram (exchange_ts vs ingest_ts).
    - `bus_occupancy_ratio`, `bus_overflow_total`.
  - **Strategy/Risk/Order**:
    - `strategy_latency_ns{strategy}` histogram (p50/p95/p99).
    - `strategy_intents_total{strategy}`.
    - `risk_reject_total{reason}`.
    - `stormguard_mode{strategy}` (gauge mapping to 0=NORMAL,…).
    - `stormguard_transitions_total{from,to}`.
    - `order_actions_total{type}` (NEW/AMEND/CANCEL).
    - `order_reject_rate`.
    - `order_latency_ns` histogram (adapter send→broker ACK).
    - `order_rate_window` gauge (10 s sliding).
  - **Execution/Positions**:
    - `execution_events_total{type}`.
    - `position_delta_latency_ns`.
    - `pnl_realized`, `pnl_unrealized` (gauges) per strategy (low-cardinality).
    - `exposure_notional`.
    - `reconciliation_duration_ms`.
    - `callback_gap_seconds`.
  - **Infra/cold path**:
    - `recorder_write_failures_total`.
    - `clickhouse_latency_ms`, `clickhouse_up` gauge.
    - `process_restart_count`.
- Label discipline: avoid high-cardinality labels (`symbol`, `strategy`) unless aggregated/bucketized (top-N via dynamic metrics or exposures per strategy only).

### Tracing (v1 optional / v2)
- Plan for OpenTelemetry instrumentation by propagating `trace_id` / `intent_id` across modules. For now, maintain correlation IDs in events/logs.
- Later add OTEL spans around major stages (feed normalization, strategy evaluation, risk checks, order send, execution callback).

### Dashboards
- **Feed & Gateway Health**:
  - Shioaji connection status, feed rates, feed gap, latency histogram, API usage vs limits.
  - Event bus occupancy and overflow counts.
  - Reconnect counts/time.
- **Strategy & Order Path**:
  - Strategy latency histograms, intents/sec, rejection counts.
  - StormGuard modes over time.
  - Order submission/ACK/reject rates, order latency.
  - Order rate vs 10 s limits.
  - Event bus health.
- **PnL & Exposure**:
  - Real-time realized/unrealized PnL (global + top strategies/symbols).
  - Drawdown, gross exposure, long vs short, leverage ratios.
  - Position snapshots summary (counts, top exposures).
- **Async Recorder/ClickHouse**:
  - Insert latency, failure counts, queue depth, availability (v1).
  - **v2**: ClickHouse background merges, part counts, pending mutations, disk usage per volume, query latency percentiles, and per-table ingestion rates.
- **System Health (v2)**:
  - Host CPU per core, memory usage, swap, NIC throughput/errors, disk IO latency, file descriptor counts, GC metrics, temperature if available.

### Alerts
- **Critical** (pager + Slack/email):
  - `feed_gap_seconds > 1` during market hours (no market data).
  - `bus_occupancy_ratio > 0.8` for >10 s or `bus_overflow_total` increases.
  - StormGuard transitions to `HALT`.
  - `order_reject_rate` > threshold (e.g., >5% over 1–5 min).
  - `order_actions_10s` or API usage hitting hard limits.
  - Shioaji WS disconnected > few seconds (market hours).
  - Async Recorder failing writes repeatedly / ClickHouse down.
- **Warning** (Slack-only):
  - Approaching rate limits (>70% of soft caps).
  - StormGuard in WARM/STORM > N seconds.
  - Strategy latency p99 > threshold (e.g., 5 ms) sustained.
  - PnL drawdown > 50–70% daily risk budget.
  - Reconciliation duration >10 s or mismatches detected.
  - ClickHouse/system issues (v2): disk usage >80%, merge backlog high, CPU/memory saturation, NIC drops.

### Data Volume & Retention
- Logs: capture order/risk/StormGuard events at INFO; degrade per-tick logs to DEBUG (off). Use log rotation and shipping (fluent-bit/vector) to ClickHouse or ELK. Retain audit logs ≥1–3 years (target 5).
- Metrics: Prometheus retention per existing infra (e.g., 15–30 days) with remote storage optional.
- Snapshots & recorder data already persisted in ClickHouse; include metadata to correlate with logs/metrics.

### Compliance & Security
- Ensure logs do not leak API keys or sensitive personal data beyond IDs required.
- Audit tables should have access controls (read-only for most, append-only service accounts).
- Config change logs must include operator identity (if CLI/gRPC supports auth).

## Non-Functional Requirements
- Minimal overhead (<5% CPU) for metrics/logging even under peak load.
- Observability components must not block hot path; asynchronous logging/metrics.
- Robust to process restarts; metrics start from zero but logs retained.
- Support remote monitoring via existing Grafana/alerting stack.

## Assumptions & Open Questions
- **Assumption**: Grafana/Prometheus stack available; log shipping agent can be installed.
- **Assumption**: ClickHouse accessible for log ingestion and metrics (if needed).
- **Open**: Confirm required retention period for audit logs (regulatory vs internal). Determine whether to integrate with existing alerting (PagerDuty) or rely on Slack/email.
