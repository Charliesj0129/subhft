# Market Data Ingestion, LOB, and Event Bus

## Problem Statement
Deliver a sub-microsecond, single-node hot path that consumes SinoPac/Shioaji (`sinotrade_tutor_md`) market data for a curated basket (≈50–200 highly liquid Taiwan equities/futures), normalizes it into a top-5 L2 limit order book per symbol, and fans strictly ordered events to in-process strategy consumers. The path must capture both exchange timestamps (`ExchTime`) and local ingest timestamps (`LocalTime`) so latency can be audited, maintain deterministic ordering, and drop rather than replay any backlog during transient failures.

## Context & Actors
- **Shioaji Broker API** – provides login/session control, REST snapshots (`market_data/snapshot.md`) and websocket streaming via `api.quote.subscribe` supporting `Tick`, `BidAsk`, and `Quote` payloads with top-5 depth (fields per `market_data/streaming/stocks.md`).
- **FeedAdapter** – single Rust process embedding the Python client, authenticates, subscribes, captures timestamps, and emits normalized events.
- **LOB Engine** – maintains in-memory per-symbol top-5 order books plus recent trade metadata.
- **In-Process Event Bus** – lock-free ring buffer (LMAX Disruptor style) pinned to a CPU core, delivering events to strategies, Async Recorder, timers, and observability hooks.
- **Timer Source** – injects deterministic `TimerTick` events (e.g., every 10 ms) for scheduling and heartbeat checks.

## Goals & Success Criteria
1. Cover curated basket within Shioaji’s subscription ceiling (200 simultaneous `api.subscribe()` calls, see `limit.md`).
2. Maintain fidelity to exchange-provided `Tick`/`BidAsk` fields, including decimal precision and metadata such as `intraday_odd`, `tick_type`, `diff_bid_vol`, etc.
3. Publish events onto the bus with <5 µs median / <20 µs p99 latency from socket read under nominal load (~200 k updates/min).
4. Each event carries both `ExchTime` (matching engine nanoseconds from payload) and `LocalTime` (captured via invariant `rdtsc` or `CLOCK_MONOTONIC_RAW`).
5. On disconnect or heartbeat timeout, halt trading, rebuild via REST snapshots (≤500 contracts per request per Shioaji docs), and resume without replaying old ticks.
6. Observability: structured logs + metrics for feed state, timestamp skew, ring-buffer utilization, and subscription health; enough detail to satisfy daily traffic audits (per `limit.md`).

## Scope
- Session management (login, token refresh) for Shioaji market data connections.
- Subscription orchestration for curated symbols and feed variants (`Tick`, `BidAsk`, optional `Quote`).
- Decoding, timestamp capture, normalization, and validation of payloads.
- LOB state management (top-5 bid/ask, last trade, aggregated volumes) including snapshot rebuild.
- Event bus design (ring buffer, strict FIFO, fail-fast on overflow) and timer injection.
- Feed health monitoring, heartbeat detection, and recovery sequencing.

## Out of Scope
- Order management, risk checks, execution processing.
- Async Recorder persistence (handled downstream), ClickHouse schema definition.
- Strategy logic or derived analytics beyond exposing normalized book/trade events.
- Cross-session symbol management (dynamic basket changes) beyond manual restart.

## Detailed Requirements
### Protocol & Data Handling
- Use Shioaji `QuoteVersion.v1` payloads for stocks/futures (fields enumerated in `market_data/streaming/stocks.md` and `futures.md`).
- Support `Tick` (trade prints), `BidAsk` (top-5 ladders), and optional `Quote` (combined view) with `intraday_odd` flag for odd-lot feeds.
- Parse decimals into fixed-point integers to avoid floating-point rounding.
- Enforce subscription upper bound (200) and daily traffic budget (500MB–10GB based on usage tiers) as per `limit.md`; adapter must refuse to subscribe beyond limit and emit clear diagnostics.

### Startup & Bootstrap Flow
1. Load curated symbol catalog (tick size, lot, exchange codes).
2. Authenticate (`api.login`) and verify total simultaneous connections <5 per account (`limit.md`).
3. Request REST snapshots via `api.snapshots` in batches ≤500 contracts per request. Apply results to seed LOB state with `ts` (nanoseconds) retained.
4. Register streaming callbacks and subscribe to requested feeds; record subscription acknowledgements (Response code 200, event 16).
5. Emit `L2Snapshot` events to event bus so strategies see the initial state before enabling trading.

### Steady-State Streaming
- **Callback Discipline**: Shioaji’s `on_tick`/`on_quote` callbacks must **only** decode the payload, capture `LocalTime`, and enqueue the raw data to an internal lock-free queue before returning. Heavy processing (LOB updates, strict ordering) must occur on a separate pinned thread.
- **Timestamping**: Capture `LocalTime` immediately upon callback entry (before decoding if possible) using invariant `rdtsc`; convert to nanoseconds relative to synchronized wall clock.
- **Ordering**: The pinned consumer thread maintains a strictly ordered handling of inbound messages; callbacks may fire concurrently, but the queue enforces linearization.
- **Processing Loop**:
  - Pop from internal queue.
  - Validate monotonic `ExchTime`; log anomaly if payload time regresses.
  - Update LOB state (top-5 arrays, `diff_*` volumes) and emit `MarketEvent` containing both timestamps.
  - Publish onto event bus via lock-free cursor.
- **Timer Source**: Timer thread posts `TimerTick` events to the same bus for scheduling/heartbeat.
- **Callback discipline**: Shioaji invokes `on_tick`/`on_quote` on its own C-extension threads that hold the Python GIL. FeedAdapter callbacks must do the absolute minimum (validate payload, stamp `LocalTime`, enqueue onto the ring buffer) and immediately return to avoid starving strategy threads or Shioaji heartbeats. Any heavier normalization occurs after the event enters our ring buffer on the pinned consumer thread.

### Backpressure & Failure
- Event bus is single-producer, multi-consumer ring buffer. If buffer is full (consumer lag), emit fatal alert, transition system to `FAIL_STOP`, and stop consuming new ticks (consistent with “fail fast” principle provided earlier).
- No persistence or replay on the hot path; Async Recorder downstream handles logging.
- Malformed payloads (missing top 5 arrays, JSON errors) are dropped with error metrics but do not halt feed unless rate exceeds threshold.

### Disconnect & Recovery
- Health monitor declares DISCONNECTED if no tick per symbol for >1 s or websocket closes unexpectedly.
- Immediately publish `FeedHalt` control event; trading policies (outside this spec) cancel active orders.
- Do not request tick replay. Instead:
  1. Re-authenticate if required, throttle login attempts to ≤1000/day limit.
  2. Request fresh snapshots, clearing LOB state beforehand.
  3. Upon snapshot application, emit `FeedResume` + `L2Snapshot` events and allow strategies to re-enable.

### Observability & Compliance
- Structured logs include: connection events, subscription counts, snapshot batch sizes, heartbeat lag, buffer utilization, timestamp skew, dropped message reasons.
- Metrics exported for Prometheus: ticks/sec per symbol, `ExchTime`−`LocalTime` delta, reconnect count, traffic usage vs Shioaji quota.
- Provide hooks to query current traffic consumption via `api.usage()` to avoid hitting usage caps (per `limit.md`).

## Edge Cases
- **Snapshot partials**: If a snapshot lacks 5 levels, fill remaining levels with zero volume and monotonic price increments; log data quality warning.
- **Odd-lot vs regular**: Distinguish by `intraday_odd` flag; odd-lot feeds may have drastically different volume scales and should not overwrite regular book state.
- **Symbol throttling**: If curated list >200, enforce priority ordering and require manual redeploy to change set.
- **Clock drift**: Monitor difference between `rdtsc`-based `LocalTime` and `CLOCK_REALTIME`; drift >100 µs triggers alert and optional feed pause until re-sync.
- **Traffic overrun**: upon approaching daily traffic cap (remaining_bytes <5%), degrade gracefully by throttling optional feeds (e.g., disable `Quote`) before broker suspends service.

## Non-Functional Requirements
- **Latency**: <5 µs median, <20 µs p99 from socket read to bus publish for trade bursts up to 5 kHz per symbol; timer injections must not exceed 1 µs jitter.
- **Throughput**: Sustain ≥200 subscriptions with combined 200 k updates/min without ring buffer overflow.
- **Reliability**: Automatic reconnect and LOB rebuild within 2 s of disconnect; snapshot batching ensures total rebuild <1 s.
- **Security**: Credentials kept in memory only; no logs containing API keys or personally identifiable data.
- **Determinism**: Single-threaded main loop pinned via CPU affinity; no garbage collection or dynamic memory after warmup.
- **Regulatory**: Adhere to subscription, traffic, and login limits documented in `sinotrade_tutor_md/limit.md`.

## References
- `sinotrade_tutor_md/market_data/streaming/stocks.md` – Tick/BidAsk/Quote payload schema.
- `sinotrade_tutor_md/market_data/snapshot.md` – Snapshot batching constraints and fields.
- `sinotrade_tutor_md/limit.md` – Usage restrictions (traffic, subscription count, login).

## Assumptions & Open Questions
- **Assumption**: Python Shioaji SDK remains the authoritative client; embedding via `pyo3` is acceptable latency-wise.
- **Assumption**: Single feed instance handles both equities and futures; if separate sessions are needed, extend spec.
- **Open**: Do we require direct futures market depth differences (e.g., different levels)? If yes, adjust LOB normalization accordingly.
