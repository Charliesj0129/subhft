# Plan – Market Data Ingestion, LOB, Event Bus

## Technology Choices
- **Language**: Rust 1.78+ (nightly allowed for inline `asm!` if needed) for deterministic, allocation-free hot path.
- **Embedded Shioaji Client**: Python 3.11 SDK (official) embedded via `pyo3` so we can leverage built-in login/subscription logic without network hops.
- **Concurrency Model**: Pinned single-threaded main loop for parsing + LOB updates; ancillary async tasks (login refresh, metrics) run on separate cores but communicate via lock-free channels.
- **Timestamping**: `rdtsc` (invariant TSC) captured in Rust; calibration service aligns to `CLOCK_REALTIME` using Chrony-synced wall clock.
- **Ring Buffer**: Custom Disruptor-style structure built on `crossbeam` atomics; capacity configurable (default 65,536 entries) with multi-consumer gating sequences.
- **Serde/Logging**: `tracing` with custom layer writing structured logs to stdout; sampling ensures logs do not violate broker traffic policies.

## Component Design
### 1. Shioaji Session Manager
- Responsible for `api.login`, token refresh, and enforcing limit of ≤5 concurrent connections/person ID and ≤1000 logins/day (`limit.md`).
- Loads curated symbol set (YAML) ensuring ≤200 entries so `api.subscribe` stays within broker cap.
- Batches snapshot requests into ≤500-contract chunks (`market_data/snapshot.md`) with throttling to respect 50-requests-per-5-seconds ceiling for data endpoints.
- Batches snapshot requests into ≤500-contract chunks (`market_data/snapshot.md`) with throttling to respect 50-requests-per-5-seconds ceiling for data endpoints.
- **Callback Discipline**: Registers callbacks for `Tick`, `BidAsk`, `Quote`. These callbacks must perform minimal work:
  1. Capture `LocalTime`.
  2. Decode basic type (or keep raw).
  3. **Immediately enqueue** to an intermediate MPSC channel.
  4. Return to release the GIL and allow Shioaji's reactor to proceed.

### 2. Timestamp & Normalization Layer (Pinned Consumer)
- **Architecture**: A separate, pinned thread (Rust or Python loop) consumes the MPSC channel.
- **Responsibilities**:
  1. Pop queue entries (Raw Data + LocalTs).
  2. Decode/Downcast Python objects (if not done partially in callback).
  3. Convert Decimal fields to fixed-point integers using tick-size metadata.
- Build `MarketEvent` structures:
  ```rust
  struct L2Level { price: i64, volume: i64 }
  enum MarketEvent {
      Tick { symbol_id, exch_ts, ingest_ts, price, size, tick_type, odd_lot },
      BidAsk { symbol_id, exch_ts, ingest_ts, bids: [L2Level;5], asks: [L2Level;5], diff_bid: [i64;5], diff_ask: [i64;5], simtrade },
      Quote { symbol_id, exch_ts, ingest_ts, ...fields },
      TimerTick { seq, ingest_ts },
      FeedControl { state },
  }
  ```
- Attach metadata: message sequence, raw topic (e.g., `QUO/v1/STK/*/TSE/2330`) for diagnostics.

### 3. LOB Engine
- Preallocates per-symbol struct holding:
  - `top_bids/top_asks`: `[L2Level;5]`
  - Last trade info (`price`, `volume`, `tick_type`)
  - Derived metrics (spread, imbalance)
  - Snapshot version counter
- Snapshot Processor: applies REST snapshots to entire struct, sets version, emits `L2Snapshot`.
- Incremental Processor: handles `BidAsk` updates by replacing entire arrays; `Tick` updates only trade metadata and total volumes.
- Validates monotonic exchange timestamps; if `exch_ts` decreases, mark symbol as `DEGRADED` and optionally pause strategy feed.

### 4. Event Bus
- Implementation: single-producer `ringbuffer::Ring` with sequences per consumer.
- Producer path:
  1. Deserialize/normalize event.
  2. Update LOB state.
  3. Acquire next slot, write final `MarketEvent` payload.
  4. Publish sequence; notify consumers via spin-wait or eventfd.
- Consumers: strategies, Async Recorder, observability service. Each maintains gating sequence; slow consumer triggers overflow detection.
- Overflow policy: emit `FeedControl { state: FailFast }`, stop feed, require manual recovery.

### 5. Timer & Heartbeat
- Use Linux `timerfd` to emit periodic `TimerTick` events pinned to same CPU core to avoid cross-core noise.
- Timer events drive:
  - Heartbeat: compare `now - last_tick_by_symbol`; >1 s triggers disconnect handling.
  - Traffic telemetry: poll `api.usage()` (<=50 data queries per 5 s) at slow cadence (e.g., every 30 s) on separate thread to avoid hot-path impact.

### 6. Recovery Flow
- State machine: `INIT → SNAPSHOTTING → CONNECTED → TRADING → DISCONNECTED → RECOVERING`.
- On DISCONNECTED:
  1. Publish `FeedControl::Halt`.
  2. Stop consuming from queue, drain outstanding events.
  3. Re-login if needed (respect rate limits).
  4. Re-run snapshot pipeline; once complete, set state to `RECOVERED`.
  5. Publish `FeedControl::Resume` and resume normal streaming.
- All state transitions logged with structured info for runbook.

## Data & Config Artifacts
- `config/symbols.yaml`: includes symbol code, exchange, tick size, lot, feed_type (regular/odd-lot).
- `contracts/market_event.md`: documents event schemas for downstream consumers.
- `config/feed.yaml`: ring-buffer size, timer interval, heartbeat thresholds, snapshot batch size.

## Error Handling & Monitoring
- Hard errors (buffer overflow, repeated malformed payloads, snapshot failure) escalate to operator via pager.
- Soft errors (single malformed payload) logged with counter; metrics exported via `/metrics`.
- Observability hooks provide:
  - Tick throughput per symbol.
  - Buffer utilization and lag per consumer.
  - Timestamp skew histograms (ExchTime vs LocalTime).
  - Reconnect attempts and durations.
- Provide CLI commands (gRPC/Unix socket) to force snapshot, change timer interval, or drain buffer for diagnostics.

## Deployment Considerations
- Build static binary + Python runtime; container image pinned to glibc version matching prod host.
- Systemd unit uses `CPUAffinity`, `LimitMEMLOCK`, and `AmbientCapabilities=CAP_NET_BIND_SERVICE` if needed.
- Provide staging mode that consumes Shioaji simulation endpoints (`simulation.md`) for integration testing.

## Testing Strategy
- **Unit Tests**: normalization logic, LOB updates, timestamp conversions.
- **Property/Replay Tests**: feed recorded data from `sinotrade_tutor_md` examples to ensure deterministic output.
- **Performance Benchmarks**: use synthetic generator to stress 5 kHz per symbol workloads.
- **Failover Drills**: simulate disconnect by closing websocket; verify state machine transitions and snapshot rebuild.

## Open Items
- Determine whether futures feeds require different lot scaling (documents suggest yes); plan includes config hook but needs confirmation.
- Evaluate whether to integrate `Quote` feed or rely solely on `Tick+BidAsk` to reduce traffic usage.
