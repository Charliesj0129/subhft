# Async Recorder & ClickHouse Cold Path

## Problem Statement
Capture a durable, query-friendly history of key trading events (market snapshots, order lifecycle, fills, positions, risk decisions) without impacting the hot path. The async recorder subscribes to bus events, micro-batches writes to ClickHouse, handles sustained rates of 10–25k rows/s (bursts up to 50k), and guarantees audit-grade persistence for orders/fills/risk data while allowing lossy handling for high-volume market data under extreme stress. The cold path must tolerate ClickHouse outages via local WAL/spool and enforce retention (short for market data, multi-year for audit tables).

## Scope
- Event ingestion from hot path (market data snapshots, orders, fills, positions/account snapshots, risk decisions, guardrail transitions, optional metrics/incidents).
- Micro-batching and ingestion pipelines per table with configurable flush criteria.
- Reliability mechanisms (bounded queues, WAL) and failure handling for ClickHouse downtime.
- ClickHouse schema design/ownership in `schemas/`.
- Retention policies and partitioning strategy.
- Interfaces to hftbacktest, analytics, dashboards via ClickHouse tables.

## Out of Scope
- Backtest/analytics logic consuming ClickHouse (covered in separate slice).
- Real-time dashboards (hot path + observability already handle real-time metrics).
- Log shipping (handled by observability logging stack).

## Data Scope & Tables
### Required Tables (existing or to be defined)
1. **`market_data`** – normalized L1/L2 snapshots and trades for backtesting:
   - Fields: `ts_exchange`, `ts_local`, `symbol`, `bid_px1`, `bid_sz1`, `ask_px1`, `ask_sz1`, optional arrays for top-5, `last_price`, `last_qty`, `source`.
   - Recording policy: event-driven snapshots (when best bid/ask changes or trade occurs) plus optional periodic sampling (10–50 ms). Avoid logging every micro-update.
2. **`orders`** – order lifecycle (audit-grade):
   - Fields: `ts_intent`, `ts_send`, `ts_ack`, `client_order_id`, `broker_order_id`, `strategy`, `symbol`, `side`, `qty`, `limit_px`, `order_type`, `tif`, `status`, `reject_reason`, `risk_mode`, `risk_decision_id`.
3. **`fills`** – executions:
   - Fields: `ts_exchange`, `ts_local`, `client_order_id`, `broker_order_id`, `fill_id`, `strategy`, `symbol`, `side`, `fill_qty`, `fill_price`, `fees`, `tax`, `source`.
4. **`positions_intraday`** – snapshots every 1–5 s:
   - Fields: `ts`, `account_id`, `symbol`, `strategy`, `position_qty`, `avg_price`, `realized_pnl`, `unrealized_pnl`, `fees_accum`, `gross_exposure`, `net_exposure`.
5. **`account_state`** – account-level snapshots:
   - Fields: `ts`, `account_id`, `equity`, `cash`, `margin_used`, `margin_available`, `day_pnl`, `risk_mode`.
6. **`risk_decisions`** – per-intent decisions:
   - Fields: `ts`, `strategy`, `symbol`, `client_order_id`, `decision`, `reason_code`, `risk_mode`, key inputs (position, notional, pnl, order_rate).
7. **`guardrail_transitions`** – StormGuard transitions:
   - Fields: `ts`, `prev_mode`, `next_mode`, `trigger_metric`, `value`, `reason`.
8. **Optional `system_events` / `metrics_timeseries`** – coarse aggregates for incidents (feed gap, bus overflow, API rate usage) if needed for offline analysis.

### Table Ownership
- All table definitions stored in `schemas/`; recorder uses migrations/versioning to keep ClickHouse in sync.
- Partitioning: default `PARTITION BY toDate(ts)` (or `ts_exchange` for market data). ORDER BY tailored per table (symbol+ts, client_order_id+ts, etc.).
- Retention:
  - `market_data`: full detail 30–90 days; aggregated or dropped thereafter.
  - `orders`, `fills`, `risk_decisions`, `guardrail_transitions`: retain 5–7 years (minimum 1–3 years if storage constrained).
  - `positions_intraday`, `account_state`: retain 1–3 years.
  - `system_events`: configurable (months).

## Performance Targets
- Sustained throughput: 10–25k rows/s across all tables; bursts up to 50k rows/s for short periods.
- Micro-batching:
  - Flush per table when batch size ≥1k–10k rows or age ≥200–500 ms.
  - Maintain separate buffers per table to avoid cross-contamination.
- Latency: target ≤1–2 s from event ingestion to ClickHouse availability; alert if >5 s.

## Reliability & Failure Handling
- Hot path decoupling via bounded in-memory queues per event class. Producers drop to best-effort policy before blocking.
- **Critical data (orders, fills, positions/account snapshots, risk_decisions, guardrail_transitions)**:
  - When queues near capacity or ClickHouse unavailable, persist to local WAL (e.g., append-only JSONL or binary chunk) stored on fast disk.
  - Background replayer streams WAL to ClickHouse when available; mark segments complete once confirmed.
  - Only drop WAL data if disk usage exceeds emergency threshold (>90%) after alerting.
- **Non-critical data (dense market_data, optional metrics)**:
  - Allowed to degrade: drop oldest or increase sampling when queue full; log + alert that data loss occurred.
- Failure modes:
  - ClickHouse outage: detect via insert errors/timeouts, switch to WAL mode, raise alert (`RecorderFailure`).
  - Recorder worker crash: supervisor restarts, replays WAL.
  - Backpressure threshold: emit observability metric (`recorder_queue_usage`) and degrade gracefully.

## Architecture Overview
- **Recorder Service**:
  - Consumes from event bus (or dedicated channels) for each event type.
  - Normalizes events to table schemas, performs lightweight transformations (e.g., flatten arrays, compute aggregated fields).
  - Maintains per-table ring buffers and WAL writer.
  - Flush worker per table handles batching and ClickHouse insertion (HTTP or native protocol).
  - Health metrics exported (queue depth, lag, insert latency).
- **Schema Manager**:
  - Applies DDL from `schemas/` to ClickHouse (checked into repo).
  - Manages TTL policies and partition maintenance (e.g., dropping old partitions, performing merges if needed).
- **Retention & Aggregation Jobs**:
  - Periodically drop expired partitions for `market_data`.
  - Optional aggregation job to downsample market data (e.g., 1s bars) before dropping raw partitions.

## Interfaces & Consumers
- **hftbacktest**: queries `market_data`, `orders`, `fills`, `positions_intraday` for simulation.
- **Analytics & Reports**: use `fills`, `positions`, `account_state`, `risk_*`.
- **Observability/Audit**: run root-cause analysis using `risk_decisions`, `guardrail_transitions`, `orders`.
- **Recovery**: Execution slice uses `positions`/`account_state` snapshots as optional reference.

## Non-Functional Requirements
- Recorder must never block or slow the hot path.
- Logging & metrics for recorder integrated into observability (queue usage, WAL size, insert errors).
- Configurable sampling/degradation policies per table.
- Secure handling of ClickHouse credentials; use TLS if available.

## Assumptions & Open Questions
- **Assumption**: ClickHouse cluster accessible with sufficient write throughput.
- **Assumption**: Disk space available for WAL (sized for at least several minutes of critical data).
- **Open**: Should market data downsampling be done inline or via downstream ETL? Need coordination with backtest team.

## Edge Cases
- Reordering/out-of-order events: recorder should rely on timestamps but not enforce strict ordering; queries can sort.
- Partition hot spots: if certain symbols produce massive data, consider future partitioning by (date, symbol_group).
- Schema evolution: adding columns requires CH `ALTER` + recorder version bump; plan migrations carefully.
