# Strategy Execution, Risk & Order Path

## Problem Statement
Provide a deterministic hot path that turns shared LOB/events into broker-ready orders within ≤1 ms, while enforcing strict risk guardrails and respecting Shioaji’s order limits. Strategies execute inside a single `StrategyRunner` thread (Python for now, Rust-ready structs later), produce `OrderIntent` objects, and pass them through a hard-stop risk engine (`Risk & StormGuard`). Approved intents are coalesced and dispatched via an `OrderAdapter` that wraps the Shioaji order APIs (`sinotrade_tutor_md/order/*`). The entire chain must remain responsive despite Python’s GIL, prevent runaway order spam, and gracefully handle rejects/timeouts.

## Context & Actors
- **StrategyRunner** – cooperative scheduler that invokes many strategies (`Strategy.on_book(ctx) → list[OrderIntents]`) sequentially on one pinned thread.
- **Risk Engine** – enforces price bands, per-symbol size/notional caps, per-strategy order-rate limits, and daily firm-wide PnL drawdown; exposes “StormGuard” soft states (NORMAL/WARM/STORM/HALT) governing throttling vs close-only behavior.
- **OrderAdapter** – converts approved intents to Shioaji orders (LIMIT/IOC/FOK, market simulated by aggressive limit), handles amend/cancel coalescing, and deals with API limits (≤250 order actions / 10 s, soft target 180).
- **Broker Order API (Shioaji)** – `api.place_order`, `api.update_order`, `api.cancel_order`; subject to traffic/login caps in `limit.md`.
- **Execution Feedback Loop** – fills/rejects feed back via execution events (other slice) but risk/adapter need synchronous acknowledgement handling.

## Goals & Success Criteria
1. Support dozens of strategies with per-event compute budgets of 100–200 µs each while staying within a ≤1 ms tick→trade SLA (Strategy + Risk + Adapter).
2. Guarantee no order leaves the system without passing:
   - Price band validation (limit vs reference/LOB spread).
   - Per-symbol size & notional caps (position + outstanding + intent).
   - Per-strategy order-rate cap (soft warning, hard stop) aligned with ≤180 actions/10 s aggregate.
   - Daily net PnL loss cap (firm & strategy) via StormGuard transitions (NORMAL→WARM→STORM→HALT).
3. OrderAdapter must aggressively coalesce amend/cancel storms: e.g., multiple price nudges collapse into one `update_order`, cancels suppressed if order already inactive.
4. Explicit handling of broker rejects/timeouts/rate-limit responses: classify as retryable (e.g., cancels) vs non-retryable (new order); surface to strategies/risk for state transitions.
5. Provide instrumentation: per-strategy latency, rejection causes, StormGuard state changes, and API usage counters.

## Scope
- Strategy invocation lifecycle and context management (shared LOB snapshot, positions, throttle feedback).
- OrderIntent schema definition (fields for side, qty, price, tif, urgency, cancel/amend semantics).
- Risk checks (hard and soft) plus StormGuard FSM.
- OrderAdapter logic: batching/coalescing, command queue to Shioaji, timeout/retry policies, rate-limit observance.
- Circuit-breaker, halt, and heartbeat signals fed back to strategies.

## Out of Scope
- Strategy algorithms themselves (user-provided).
- Execution/position reconciliation (handled by execution slice).
- Long-term analytics or offline risk modeling.

## Detailed Requirements
### StrategyRunner
- Single OS thread pinned to a dedicated core (like event bus). Executes strategies sequentially for every `MarketEvent` shipped from the bus.
- Each strategy receives immutable context: current LOB (shared pointer), positions, recent fills, StormGuard state, and helper APIs to request throttles/metrics.
- Strict budget monitoring: `Strategy.on_book` must return within 100–200 µs; Runner records latency histograms and can auto-disable a strategy if it exceeds budget repeatedly.
- Output is `list[OrderIntent]` with structured fields: `intent_id`, `strategy_id`, `symbol`, `side`, `price`, `qty`, `tif` {LIMIT, IOC, FOK}, `aggression` (for “market via crossing limit”), `intent_type` {NEW, AMEND, CANCEL}, `target_order_id` for modifications.
- FFI-friendly layout (contiguous memory, plain types) so future Rust strategies can attach without refactor.

### Risk Engine & StormGuard
- Hard checks:
  - **Price bands**: price must be within [reference ± configurable ticks] and inside exchange limit up/down bounds (available via LOB & Shioaji contract info).
  - **Per-symbol size**: `abs(position + outstanding + intent_qty)` ≤ configured cap; notional cap computed as `price * qty`.
  - **Order-rate**: maintain sliding window (10 s) per strategy & global; if >soft limit (e.g., 180) trigger WARM, at hard limit reject new intents until window clears.
  - **PnL loss cap**: when strategy or global realized PnL drops below thresholds, escalate StormGuard state.
- StormGuard states:
  - `NORMAL`: standard operation.
  - `WARM`: apply throttles (e.g., reduce max qty, widen price bands).
  - `STORM`: allow only risk-reduced sizing and mark new position-increasing orders as close-only.
  - `HALT`: only allow flattening intents; all other intents rejected and StrategyRunner notified.
- Soft controls adjust exposures gradually; hard controls result in immediate rejection with reason codes returned to strategies.
- Events: every state change logged + published to event bus for dashboards.

### OrderAdapter
- Maintains async FIFO command queue decoupled from StrategyRunner to keep ≤1 ms budget; sends commands via Shioaji Python SDK (like feed).
- Supported order types: `LIMIT`, `IOC`, `FOK`; “market” implemented by setting aggressive price (best opp ± safety ticks) derived from LOB and verifying against price bands.
- Coalescing:
  - Rapid successive AMENDs on same `order_id` collapse into latest parameters before hitting broker, provided no acknowledgement yet.
  - CANCEL requests suppressed if order already in terminal state.
  - Batch CANCEL on StormGuard HALT to minimize API calls.
- Rate limiting:
  - Track sliding window for API actions; throttle new commands when approaching 180/10 s soft limit and cut off before 250/10 s broker hard limit (per `sinotrade_tutor_md/limit.md`).
  - Provide backpressure to strategies (intent queue saturates → `StrategyRunner` gets `OrderThrottle` signal to self-throttle).
- Error handling:
  - **Reject (business)**: propagate to strategy with reason (e.g., price limit). Risk engine increments violation counters.
  - **Timeout**: treat NEW as failure (require manual strategy retry); CANCEL/AMEND may retry once if within rate budget, else surface to ops.
  - **Rate-limit response**: transition StormGuard to WARM or HALT, alert ops, pause submissions until window clears.
  - **Circuit breaker**: internal state that halts submissions and flushes queue while ensuring cancel-only mode engages.

### Latency & Concurrency
- Tick→Strategy→Risk→Adapter path must remain ≤1 ms median, ≤2 ms p99 (extra headroom vs 1 ms SLA). Strategy execution budgets enforced; Risk + Adapter per-intent processing must be <100 µs combined under nominal load.
- Command queue uses lock-free ring buffer between StrategyRunner and OrderAdapter worker to avoid GIL contention.
- All callbacks and Shioaji interactions minimize GIL hold; heavy serialization happens on dedicated worker threads.

### Telemetry & Controls
- Metrics: per-strategy intent counts, rejections by reason, StormGuard dwell time, order API usage, latency breakdown (Strategy vs Risk vs Adapter), outstanding orders by symbol.
- Structured logs for every risk reject, StormGuard transition, broker error, and rate-limit event (include `intent_id`, `strategy_id`, `symbol`, `state`).
- Operator commands: toggle Strategy enablement, force StormGuard state, trigger cancel-all; accessible via CLI/gRPC with audit logging.

## Main Flows
1. **Normal tick flow**: StrategyRunner loops through strategies → collects intents → Risk engine validates → OrderAdapter queues & sends → broker ACK → execution events (handled elsewhere) update positions.
2. **StormGuard escalation**: On drawdown threshold, state escalates to STORM → strategies receive `close-only` flag, risk rejects position-increasing intents; OrderAdapter prioritizes flattening cancels.
3. **Rate-limit nearing**: API usage hits 80% of 180 limit → Adapter signals StrategyRunner to slow submissions, risk enforces lower order-rate cap; at 100% soft limit, new position increases blocked until window clears.
4. **Broker reject**: Adapter receives reject → surfaces to Risk (counts violation) and Strategy (optionally adapt). If repeated rejects for same symbol, StormGuard may auto-warm.
5. **Timeout/circuit break**: Adapter misses heartbeat or receives transport error → triggers circuit breaker, transitions to HALT, stops NEW orders, issues cancel-all, notifies strategies.

## Edge Cases
- Strategy exceeding compute budget: auto-disable strategy, raise alert, optionally move to quarantine list until manual intervention.
- Intent referencing unknown order_id (AMEND/CANCEL) – treat as reject, log, and optionally request full order status refresh.
- LOB gaps (no liquidity) – Risk may adjust price bands; market-crossing intents must not exceed price limits.
- Broker-maintenance windows – Adapter should detect login or endpoint downtime and pre-emptively halt submissions.

## Non-Functional Requirements
- **Determinism**: StrategyRunner ordering is fixed; intents processed FIFO per strategy.
- **Safety first**: Any internal error defaults to HALT/close-only rather than continuing to trade unsafely.
- **Auditability**: Every intent carries a reason code for acceptance/rejection; logs correlate with broker order IDs.
- **Extensibility**: All interfaces designed for eventual Rust implementation without behavior drift.

## References
- `sinotrade_tutor_md/limit.md` – order API rate limits and connection quotas.
- `sinotrade_tutor_md/order/*.md` – order creation/update/cancel semantics, supported TIF/price types.

## Assumptions & Open Questions
- **Assumption**: Strategies are synchronous functions today; future async/fiber execution will reuse same contracts.
- **Assumption**: Daily PnL data resolved from execution slice; risk engine receives timely updates.
- **Open**: Need concrete mapping between StormGuard states and throttle parameters (percent reductions, close-only rules) – to be defined with trading team.
