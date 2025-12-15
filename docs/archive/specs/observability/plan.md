# Plan – Observability & Audit Logging

- **Logging**: `structlog` JSON → local file rotation → shipped via `fluent-bit`/`vector` to ClickHouse (`logs_hft` or per-domain tables) or alternative sinks (ELK/Loki). Implement structured field template across services.
- **Metrics**: `prometheus_client` exposer in HFT process (e.g., `uvicorn` or built-in HTTP server) at `/metrics`, plus exporters for ClickHouse (`clickhouse_exporter`) and host system metrics (`node_exporter` or psutil-based sidecar). Grafana dashboards consume Prometheus data.
- **Tracing**: prepare for OpenTelemetry by propagating `trace_id`/`intent_id`; optional OTEL collector integration later.
- **Alerting**: Prometheus alert rules feeding Alertmanager → Slack + pager/email. Critical vs warning severities.

## Components
### 1. Logging/Audit Infrastructure
- Define structured log schema (JSON) for each component:
  - `market_data`, `strategy_runner`, `risk_engine`, `order_adapter`, `execution`, `recorder`, `monitor`.
- Implement logging helpers that enforce key fields and log levels.
- Configure log rotation (size- or time-based) and shipping agent to ClickHouse/ELK.
- Append-only ClickHouse tables:
  - `audit_orders` (order intents + broker responses).
  - `audit_risk_decisions`.
  - `audit_guardrail_transitions`.
  - `audit_connectivity`.
- Include config-change logging (CLI/gRPC commands log operator ID, config hash).

### 2. Metrics Instrumentation
- Build metrics registry module exposing:
  - Counters/gauges/histograms listed in spec (feed, strategy, risk, order, execution, infra).
  - Provide helper functions for components to record metrics without coupling.
- Implement `/metrics` endpoint, ensure served from separate thread/event loop to avoid blocking hot path.
- Apply low-cardinality guidelines (aggregate by component, not individual symbol unless necessary).
- Example metrics definitions:
  - `feed_events_total = Counter("hft_feed_events_total", "...", ["type"])`
  - `bus_occupancy_ratio = Gauge("hft_bus_occupancy_ratio", "...")`
  - `strategy_latency_ns = Histogram("hft_strategy_latency_ns", buckets=[...], labelnames=["strategy"])` (with curated strategy labels).
  - `stormguard_mode = Gauge("hft_stormguard_mode", "...", ["strategy"])`.
  - `order_latency_ns = Histogram(...)`.
  - `recorder_write_failures_total`, `clickhouse_up`.

### 3. Dashboards
- **Feed & Gateway Health Dashboard**
  - Panels: connection status, feed rates, feed gap, latency histogram, API usage vs limits, event bus occupancy/overflow, reconnect counts.
- **Strategy & Order Path Dashboard**
  - Panels: strategy latency histograms, intents/sec, risk rejects (stacked), StormGuard state timeline, order submission/ACK/reject series, order latency, order rate vs limit, event bus health.
- **PnL & Exposure Dashboard**
  - Panels: realized/unrealized PnL (global & top strategies), drawdown, gross/long/short exposure, leverage ratio, top position table.
- **Recorder/ClickHouse Dashboard**
  - Panels: recorder write success rate, latency, ClickHouse availability, ingestion queue depth.
  - **v2**: add ClickHouse exporter metrics (background merges, part counts, pending mutations, insert/query latency, disk usage per volume, CPU/IO per ClickHouse process).
- **System Health Dashboard (v2)**
  - Panels: node exporter CPU per core, load average, memory & swap, NIC throughput and error counters, disk IO latency & queue, filesystem usage, Python process RSS/GC stats, temperature sensors if exposed.
- Build Grafana JSON definitions committed under `dashboards/`.

### 4. Alerting Rules
- Define Prometheus alert rules (YAML) grouping WARN vs CRIT:
  - `FeedGapCritical`: `feed_gap_seconds > 1` for >5 s during trading hours.
  - `BusOverflowCritical`: `bus_occupancy_ratio > 0.8` for >10 s or `bus_overflow_total` increase >0.
  - `StormGuardHaltCritical`: `stormguard_mode == HALT`.
  - `OrderRejectSpikeCritical`: `rate(order_reject_total[1m]) / rate(order_actions_total[1m]) > 0.05`.
  - `ShioajiDisconnectCritical`: `shioaji_connected == 0`.
  - `RecorderFailureCritical`: `increase(recorder_write_failures_total[5m]) > 0`.
  - WARN-level analogues for approaching limits, high latency, StormGuard WARM/STORM, PnL drawdown thresholds, reconciliation delays.
- Route CRIT to Slack+#trading-alerts and pager/email; WARN to Slack only.

### 5. Telemetry Storage & Retention
- Configure ClickHouse tables with TTL (e.g., 2–3 years for audit logs).
- Prometheus retention per cluster policy (15–30 days) with optional remote storage.
- Log rotation + shipping ensures disk doesn’t fill; include monitoring on log shipper status.

### 6. Process Health & Infra Checks
- Implement basic health endpoints or heartbeat metrics for:
  - Async Recorder (success/failure counters, queue depth).
  - ClickHouse connectivity probe (periodic ping) plus exporter metrics for insert latency, merges, part count.
  - Main HFT process liveness (watchdog metric `process_uptime_seconds`).
- Deploy/promote system exporters:
  - **ClickHouse exporter**: scrape internal tables (system.metrics, system.events).
  - **Node exporter or psutil agent**: CPU/memory/disk/NIC, temperature.
  - Optional `process_exporter` for per-process resource usage (HFT engine, recorder).

### 7. Correlation IDs & Tracing Prep
- Generate `correlation_id` / `intent_id` propagated from strategy intent → risk → order → execution. Include in logs and metrics labels (where safe).
- Provide utilities to start OTEL spans in future (no-op for now) so enabling tracing is straightforward.

### 8. Documentation & Runbooks
- Document metric definitions, log schema, dashboards, alert rules, and operator responses.
- Runbook entries:
  - “Feed gap alert” – steps to check Shioaji connection, event bus.
  - “StormGuard HALT” – review risk logs, check PnL, resume procedures.
  - “Recorder failure” – verify ClickHouse status, restart recorder.
  - Config change logging & verification procedure.

## Implementation Steps
1. **Schema & Config** – finalize log/metric definitions, dashboard requirements (T1 tasks below).
2. **Logging & Metrics Hooks** – instrument each component (Market Data, Strategy, Risk, Order, Execution, Recorder) with standardized logging and metrics.
3. **Endpoints & Exporters** – add `/metrics`, log shipping, ClickHouse tables.
4. **Dashboards & Alerts** – build Grafana dashboards, Prometheus alert rules, configure Alertmanager routing.
5. **Audit Retention & Compliance** – configure ClickHouse TTL, access controls, retention policies.
6. **Docs & Runbooks** – write operator guides.

## Testing
- **Unit tests** for logging helpers to ensure required fields present.
- **Integration tests** for metrics endpoint (expose expected metrics).
- **Alert tests** using Prometheus `unit` mode or `amtool` to verify rule triggers.
- **Load tests** to ensure observability overhead <5% CPU under high throughput.
- **Failure drills** simulating feed gaps, StormGuard HALT, recorder failures to confirm alerts fire and runbooks work.
