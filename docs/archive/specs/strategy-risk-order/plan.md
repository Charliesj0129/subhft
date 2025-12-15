# Plan – Strategy Execution, Risk & Order Path

## Technology Choices
- **Runtime**: Python 3.11 for strategies/Risk/OrderAdapter in Phase 1 (aligns with existing market-data fallback). All interfaces use FFI-friendly dataclasses/ctypes so Rust parity can swap in without changes.
- **Scheduling**: Single `StrategyRunner` thread pinned via `psutil`/`os.sched_setaffinity`; uses cooperative loop driven by event bus notifications.
- **Data Structures**: 
  - `OrderIntent` defined via `attrs`/`pydantic` with memoryview-backed buffers for zero-copy transfer into Rust later.
  - Sliding-window metrics implemented via lock-free ring buffers / `deque` with timestamped counts.
- **Risk Engine**: Python module with vectorized checks using `numpy` for price bands/notional calculations; critical sections executed under per-strategy locks to avoid GIL thrash (but mostly single thread).
- **OrderAdapter**: Python worker thread using Shioaji SDK; command queue implemented with `multiprocessing.shared_memory` ring or `queue.SimpleQueue` + `cython` accelerator; pluggable to Rust in future.
- **Config**: YAML in `config/strategy_limits.yaml` (per-strategy caps, StormGuard thresholds) and `config/order_adapter.yaml` (rate limits, coalescing windows).

## Component Architecture
### 1. StrategyRunner
- Consumes `MarketEvent` references from event bus consumer cursor (read-only).
- Maintains registry of strategies (Python callables) with metadata: `strategy_id`, compute budget, StormGuard overrides.
- Loop:
  1. Pull event.
  2. For each enabled strategy:
     - Build `StrategyContext` (struct with pointers to LOB snapshot, recent fills, StormGuard state, throttle hints).
     - Invoke `on_book`.
     - Measure duration via `time.perf_counter_ns`; update latency histogram, check budget.
     - Collect `OrderIntent`s; annotate with timestamp and sequence.
- Push intents into `risk_queue` (lock-free MPSC). If queue full, mark strategy throttled and drop intents with reason `queue_backpressure`.

### 2. Risk & StormGuard Engine
- Runs on dedicated worker thread to keep StrategyRunner lightweight.
- Fetches intents FIFO, groups by strategy for context lookups.
- Hard checks implemented as composable validators:
  - `PriceBandValidator`: uses LOB + config.
  - `SizeNotionalValidator`: uses positions/outstanding from execution service (shared memory snapshot).
  - `OrderRateValidator`: sliding-window counter per strategy + global.
  - `PnLValidator`: compares to thresholds; interacts with StormGuard FSM.
- StormGuard FSM:
  - Maintains per-strategy + global states.
  - Config-driven thresholds: e.g., WARM at −0.5% PnL, STORM at −1%, HALT at −1.5%.
  - State transitions emit events (via bus) and adjust validator parameters (e.g., multiplier on max size).
- Outputs either `RiskDecision::Approve` (with sanitized intent) or `RiskDecision::Reject` (with reason code) into `order_queue`.
- Rejects sent back to StrategyRunner via side channel to update strategy state.

### 3. OrderAdapter Worker
- Dedicated worker thread (or future Rust task) consuming from `order_queue`.
- Maintains map of live orders (strategy_id, broker trade object, timestamps).
- Command processing:
  1. Intent classification (NEW/AMEND/CANCEL).
  2. Coalescing logic:
     - Use `pending_updates[order_id]` to absorb rapid AMEND series until either ack received or coalescing window (e.g., 5 ms) expires.
     - CANCEL deduplicated; StormGuard HALT triggers bulk-cancel routine iterating outstanding map.
  3. Rate limiter:
     - Sliding window (10 s) tracked via `deque` of timestamps; approaching 80% of soft cap -> signal to risk to reduce order-rate; hitting 100% -> queue paused until window clears.
  4. Serialize to Shioaji calls (`api.place_order`, `api.update_order`, `api.cancel_order`).
- Response handling:
  - Success: capture `trade.id/ordno`, forward to execution service, update outstanding map.
  - Reject: annotate with `reason_code`, propagate to StrategyRunner + risk.
  - Timeout: cancel outstanding future via `asyncio`/Timer; NEW treated as fail (Strategy must regenerate), CANCEL/AMEND retried once if rate budget allows.
  - Rate-limit error: trigger StormGuard WARM or HALT and open circuit breaker; resume when safe.
- Circuit Breaker:
  - `state = CLOSED → OPEN` on repeated timeouts/rejects; `OPEN` halts new submissions, kicks off cancel-all; `HALF_OPEN` tests with single order when broker stable again.

### 4. Interfaces & Data Flow
- `OrderIntent` struct stored in shared memory or `numpy` array enabling zero-copy pass to Rust later.
- `RiskDecision` struct annotated with rejections/resolution.
- `OrderCommand` struct for adapter includes coalesced parameters, `deadline_ns`, `stormguard_state`.
- Execution events and positions provided via shared caches (populated by execution slice).

## Error Handling & Observability
- Wrap every strategy invocation in try/except; exception disables strategy and triggers alert.
- Metrics exported via Prometheus:
  - `strategy_latency_ns`, `intents_total`, `risk_reject_total{reason}`, `stormguard_state`, `order_actions_count`.
  - `tick_to_trade_latency` computed from `ingest_ts` (from market data) to order dispatch time.
- Structured logging using `structlog`: include `intent_id`, `strategy_id`, `reason`, `stormguard_state`.
- Alert thresholds:
  - Strategy budget violation >3 times/min.
  - StormGuard HALT triggers pager.
  - API usage >150 actions/10 s warns; >180 triggers auto-slow; >230 immediate HALT.

## Deployment & Ops
- Config hot-reload: watchers on YAML files to update limits without restart (with validation).
- Provide CLI to:
  - Enable/disable strategy.
  - Force StormGuard state.
  - Dump outstanding orders / risk state.
  - Trigger cancel-all and hold fire.
- All state snapshots persisted to shared memory for other processes (e.g., dashboard).

## Testing Strategy
- **Unit tests**: validators, StormGuard FSM transitions, coalescing logic.
- **Simulation tests**: feed recorded tick streams into StrategyRunner stub strategies to verify latency budgets and rejection reasons.
- **Rate-limit tests**: stress order adapter until hitting soft cap to ensure throttling works without broker ban.
- **Failure injection**: simulate broker rejects/timeouts to confirm circuit breaker behavior and close-only states.

## Migration to Rust (Future)
- Keep `OrderIntent`, `RiskDecision`, `OrderCommand` definitions in shared `contracts` so Rust components can be swapped by implementing same trait (FFI boundary via `cbindgen` later).
- Python StrategyRunner remains reference implementation; once Rust strategies ready, host can run foreign strategies via FFI pointers without changing risk/adapter semantics.
