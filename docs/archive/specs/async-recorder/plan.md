# Plan – Async Recorder & ClickHouse Cold Path

## Architecture & Components
1. **Event Intake Layer**
   - Subscribe to event bus channels (market snapshots, orders, fills, positions/account snapshots, risk decisions, guardrail transitions).
   - Use lightweight readers that enqueue normalized records into per-table buffers (ring buffers or queues).
   - Apply table-specific sampling logic (e.g., dedup best bid/ask events).

2. **Normalization & Serialization**
   - Map hot-path structs to ClickHouse row dictionaries / columnar batches matching schemas in `schemas/`.
   - Flatten L2 arrays or store as `Array` columns per schema.
   - Append metadata (source, environment) and ensure timestamps are consistent (UTC, nanoseconds).

3. **Batcher/Flusher**
   - For each table, maintain in-memory batch buffers (column-oriented).
   - Flush criteria:
     - row count ≥ configurable threshold (default 2,000–5,000 for market_data; 100–500 for low-volume tables).
     - time since last flush ≥ 250–500 ms.
   - Use ClickHouse HTTP or native client with compressed columnar payloads (e.g., `INSERT INTO ... FORMAT Parquet/JSONEachRow`).
   - Track per-table insert latency and row counts; expose metrics.

4. **WAL & Replay Mechanism**
   - Critical tables (orders, fills, positions, account_state, risk_decisions, guardrail_transitions):
     - When ClickHouse insert fails or buffer queue exceeds threshold, write batches to local WAL segments (e.g., `.wal/orders/segment-<ts>.bin`).
     - WAL format: length-prefixed compressed batches or JSONL with metadata.
     - Replay worker scans WAL directories when CH is healthy, replays in order, deletes segments upon success.
     - Maintain WAL size metrics and enforce retention (alert when > configurable GB).
   - Non-critical tables (market_data, optional metrics):
     - On failure, apply best-effort policy: drop oldest batches, increase sampling, log warning.

5. **Failure Detection & Backpressure**
   - Monitor ClickHouse insert errors/timeouts; mark state `DEGRADED` or `DOWN`.
   - Expose metrics:
     - `recorder_queue_usage{table}`
     - `recorder_insert_latency_ms{table}`
     - `recorder_wal_size_bytes`
     - `recorder_dropped_rows_total{table}`
   - If queue usage > threshold, trigger degradation: e.g., drop or downsample market data.
   - Provide CLI to inspect queue depth, WAL status, force flush, and purge old WAL segments (with safeguards).

6. **Schema Management & Retention**
   - Maintain ClickHouse DDL scripts in `schemas/`.
   - Deploy migrations via automation (e.g., `clickhouse-client -n < schemas/all.sql`).
   - Use `PARTITION BY toDate(ts)` (or `toDate(ts_exchange)` for market_data) and appropriate `ORDER BY`.
   - Configure TTLs:
     - `market_data`: TTL 30–90 days; optional aggregated tables for longer storage.
     - Audit tables: TTL 5–7 years (or manual retention process).
   - Implement partition maintenance job to drop expired partitions and optionally aggregate older data.

7. **Integration with Observability**
   - Recorder exposes metrics and structured logs (insert success/failure, WAL actions).
   - Alerts for:
     - Insert latency > 5 s.
     - WAL growth beyond threshold.
     - Dropped rows (non-critical data) exceeding limit.
     - ClickHouse unreachable.
   - Dashboard panels for recorder queue depth, insert rates, WAL size, ClickHouse health.

8. **Security & Deployment**
   - Store ClickHouse credentials securely (env vars, secret manager). Use TLS if CH supports.
   - Deploy recorder as separate process/service with restart supervision.
   - Ensure recorder uses CPU/memory quotas to avoid interfering with hot path (e.g., separate core).
   - Provide configuration file for per-table sampling, batch sizes, WAL directory, retention.

## Implementation Steps
1. **Schema Finalization** – ensure `schemas/` includes DDL for all required tables (market_data, orders, fills, positions_intraday, account_state, risk_decisions, guardrail_transitions, optional system events).
2. **Event Intake & Normalization** – implement channel listeners and mappers for each event class.
3. **Batcher/Flusher** – create per-table batch buffers and ClickHouse insert routines with retry logic.
4. **WAL Mechanism** – implement WAL writer/replayer for critical tables; integrate with failure detection.
5. **Sampling/Degradation Logic** – add dedup/sampling for market_data and degrade strategy when queue full.
6. **Config & CLI** – add config files referencing batch sizes, flush intervals, sampling policies, WAL paths; CLI for status/replay.
7. **Observability Hooks** – integrate metrics/logging per observability plan.
8. **Testing & Validation** – simulate high-load, ClickHouse outages, WAL replay, and verify data integrity.

## Testing Strategy
- **Unit tests**: normalization schemas, batch flushing logic, WAL encode/decode.
- **Integration tests**:
  - Write to test ClickHouse instance, verify data matches schema.
  - Simulate ClickHouse downtime: ensure WAL captures data, replay works.
  - Throughput tests producing 25k rows/s to ensure recorder keeps up.
- **Resilience tests**: artificially fill queues, trigger degradation, confirm alerts/logs.
- **Retention tests**: run TTL job on test data to ensure partitions drop correctly.

## Open Questions
- Should downsampling (e.g., 1s bars) be done inline or via downstream ETL? (Coordinate with analytics.)
- How large should WAL storage be (based on expected outage durations)?
- Do we need encryption at rest for WAL/ClickHouse data?
