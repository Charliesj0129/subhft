# Plan – Execution Events & Position State

## Technology & Runtime
- **Language**: Python 3.11 to stay aligned with existing pipeline; interfaces defined via dataclasses with optional `cffi`/`ctypes` to enable future Rust migration.
- **Threading Model**:
  - Shioaji callbacks run on broker-controlled threads; Python handlers only enqueue events.
  - Execution Normalizer + PositionStore runs on dedicated worker thread pinned to CPU core (optional) to ensure ≤10 ms SLA.
  - Snapshot/reconciliation worker handles polling APIs (limited to 25 portfolio queries / 5 s per `limit.md`).
- **Data Stores**: In-memory `PositionStore` using `dict`/`pandas`-like structures with lock-free read snapshots (RCU pattern) for StrategyRunner/Risk; periodic snapshots serialized to `Arrow`/`Parquet` for recorder.
- **Configs**: `config/execution.yaml` defining snapshot intervals, reconciliation thresholds, odd-lot handling, and API polling cadence.

## Component Design
### 1. Callback Bridge
- Register `on_order` and `on_deal` Shioaji callbacks (or equivalent) through Python decorator interface.
- Each callback performs:
  1. Extract relevant fields (order status, deal info) without heavy conversions.
  2. Capture ingest timestamp via `time.time_ns`.
  3. Push a compact `RawExecEvent` struct into a lock-free queue consumed by normalizer.
- Heartbeat timer ensures callback flow is alive; absence for >X ms triggers reconciliation poll.

### 2. Execution Normalizer
- Dedicated worker pops `RawExecEvent`, joins with local metadata (intent registry mapping `ordno`→`strategy_id`, `symbol`, etc.), and emits normalized events:
  - `OrderEvent`: status transitions, outstanding qty updates.
  - `FillEvent`: fill details with fees/taxes.
- Detect duplicates via `(ordno, seqno, match_ts)`; ignore duplicates while logging occurrence.
- Publish normalized events to:
  - In-process event bus (for strategies/risk).
  - Async Recorder channel (for ClickHouse ingestion).
  - PositionStore update queue.

### 3. PositionStore
- Maintains per-key entries using `dataclasses` stored in dictionaries keyed by `(account, strategy, symbol)`.
- Updates:
  - On fill: adjust qty, avg price, realized PnL (`(fill_price - avg_price)*qty`), fees, exposure.
  - On market move: update unrealized PnL when LOB mid changes (subscribe to bus for price ticks).
  - On cancel: update outstanding order metrics (if tracked here).
- Provides thread-safe read snapshots via copy-on-write or `threading.RLock`+versioning; StrategyRunner/Risk get references updated inline.
- Snapshot emitter:
  - Delta emitter pushes `PositionDelta` events on each update.
  - Periodic snapshots (e.g., every 1 s) captured via background task and sent to recorder/dashboards.

### 4. Reconciliation Service
- Handles startup recovery and runtime corrections.
- **Startup**:
  - Call `list_positions`, `list_profit_loss`, `list_settlements`, `list_profit_loss_detail` (subject to `limit.md` 25 calls / 5 s limit; implement pacing).
  - Populate PositionStore and outstanding order map with broker truth.
  - Optionally query Async Recorder/ClickHouse for last persisted snapshot to compare; log diffs.
- **Runtime**:
  - Monitor callback heartbeat; on gap, trigger targeted `update_status` or `list_positions` to reconcile affected symbols.
  - Provide CLI command `exec reconcile` to force snapshot/polled data ingestion.
- Ensure entire recovery completes within 5–10 s: parallelize API calls where allowed, and preallocate data structures.

### 5. Integration Points
- **StrategyRunner/Risk**: subscribe to execution events and fetch current positions via shared snapshot.
- **OrderAdapter**: uses normalized order events to track outstanding orders, ensuring consistency between adapter and broker.
- **Async Recorder**: receives both streaming events and periodic snapshots; uses schema defined in `schemas/execution.sql`.
- **Dashboards/API**: future gRPC/HTTP service reading from PositionStore snapshot or ClickHouse.

## Error Handling & Observability
- Structured logs:
  - Callback errors, dedup events, reconciliation results, mismatches vs broker.
  - Position adjustments with reason (callback vs snapshot correction).
- Metrics:
  - `execution_events_total` by type.
  - `position_delta_latency_ms` (callback TS to risk visibility).
  - `reconciliation_duration_ms`.
  - `callback_gap_seconds`.
- Alerting:
  - No execution callbacks for >1 s during market hours → alert.
  - Reconciliation mismatch > threshold (qty or PnL) → alert.
  - Recovery duration >10 s → alert before resuming trading.

## Deployment & Ops
- Provide CLI/gRPC endpoints:
  - `exec status` – view outstanding orders, last callback time.
  - `exec snapshot` – dump current positions.
  - `exec reconcile` – trigger broker snapshot refresh.
- Ensure snapshot API usage respects broker limits (throttle to 25 calls/5 s; implement exponential backoff).
- On shutdown, persist last snapshot to disk (optional) for quicker restart if broker unavailable.

## Testing Strategy
- **Unit tests**: normalization logic, PnL calculations, dedup detection.
- **Integration tests**: simulate callback streams with recorded data; verify latency and PositionStore accuracy.
- **Recovery tests**: mock broker API responses to ensure startup rebuild populates correct positions and completes within SLA.
- **Mismatch tests**: introduce artificial discrepancies to confirm reconciliation generates corrections and alerts.

## Future Extensions
- Expose read-only API endpoints for dashboards.
- Mirror PositionStore into ClickHouse in near real-time for complex analytics.
- Add support for multiple broker sessions or asset classes (futures/options) by abstracting contract metadata.
