# StrategyRunner – Spec

## Problem Statement
Provide a deterministic, single-threaded orchestrator that consumes bus events, builds strategy contexts (including LOB features and positions), executes Python strategies within strict latency budgets, and emits order intents into the risk pipeline with observability hooks, error isolation, and configuration-driven strategy management. Strategy APIs must align with Shioaji semantics (e.g., order attributes, symbol identifiers) so intents can be routed through Shioaji-compatible adapters without translation loss.

## Requirements
### Inputs
- Market events from Event Bus (Tick/BidAsk/Snapshot/Feature/Timer).
- Position updates from PositionStore.
- Configuration describing enabled strategies, parameters, priority.

### Outputs
- `OrderIntent` objects enqueued to risk queue.
- Metrics/logs for strategy latency, errors, disablement.
- Optional diagnostics (per-strategy throttling, state snapshots).

### Functional Requirements
1. **Registration & Configuration**
   - Support dynamic strategy registration via config or CLI (strategy id, module.class, params, budget, symbol filters).
   - Ensure strategies declare compatible product types (stocks, futures) to match Shioaji contract metadata.
   - Allow hot enable/disable and parameter reload with minimal downtime.
2. **Context Building**
   - For each event, assemble `StrategyContext` with references to LOB state/features, position snapshots, StormGuard state, timer info.
   - Provide helper APIs (`ctx.get_lob(symbol)`, `ctx.get_features(symbol)`, `ctx.place_order(...)` wrappers).
3. **Execution Loop**
   - Single pinned thread consumes bus events sequentially, invokes strategies respecting budgets (100–200 µs).
   - Catch exceptions per strategy, log, disable faulty strategy to avoid cascading failures.
4. **Intent Handling**
   - Validate intents (symbol exists, qty >0, TIF/order type supported by Shioaji per `sinotrade_tutor_md/order/*.md`), timestamp them, and enqueue to risk queue.
   - Apply per-strategy intent rate limits; drop or throttle when risk queue is full with proper logging.
5. **Timer Integration**
   - Recognize TimerTick events and route to strategies (e.g., `on_timer` or via context flag), enabling periodic logic without blocking.
6. **Concurrency & Isolation**
   - Ensure strategies cannot block event loop; optionally support concurrency via cooperative scheduling (future work).
7. **Observability**
   - Metrics: latency histogram per strategy, intents count, error count, disablement state.
   - Logs: structured entries for registration, errors, disablement, queue backpressure.

### Non-Functional
- Latency: event processing should not exceed configured per-strategy budget; overall loop handles >100k events/s.
- Reliability: auto-disable misbehaving strategies; support manual restart.
- Configurability: YAML configuration for strategy list, budgets, enable flags.

### Deliverables
- Updated `strategy/runner.py` implementing above behavior.
- `StrategyContext` enhancements and helper APIs.
- Config loader & CLI for managing strategies.
- Tests covering context injection, latency enforcement, error handling.
- Config file format documented (`config/strategies.yaml`).
