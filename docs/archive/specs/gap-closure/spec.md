# Gap Closure – Architecture vs Implementation

## Problem Statement
The current codebase implements the high-level architecture only partially. Key flows (strategy context wiring, order adapter ↔ broker, execution reconciliation, recorder/ClickHouse integration, backtest runtime, Rust feature engine, and user onboarding) remain stubs. We need to close these gaps so that the system matches the diagrammed Online/Offline/Observability slices and provides a usable Python strategy API backed by Rust-accelerated features.

## Identified Gaps
### 1. Strategy Context & Feature Access
- `StrategyRunner` still instantiates `StrategyContext` with `lob=None` and empty positions, so strategies cannot consume LOB or PositionStore data.
- No feature-engine interface exposes mid/spread/imbalance or derived signals; `LOBEngine` maintains raw levels only.
- Missing stable strategy API (helpers to place orders, access features, register strategy configs).

### 2. Order Adapter & Broker Integration
- `OrderAdapter` logs intent handling but lacks `live_orders` state, Shioaji API calls, coalescing, and ACK tracking.
- No feedback loop from broker responses back to adapter/risk (thus circuit breaker/rate limits ineffective).

### 3. Execution Mapping & Reconciliation
- `ExecutionNormalizer` emits `strategy_id="UNKNOWN"` because there is no order_id → strategy map.
- `ReconciliationService` is a skeleton; portfolio snapshots are never fetched/applied.
- Position deltas not persisted to recorder or fed back into risk state (StormGuard PnL).

### 4. Recorder / ClickHouse Integration
- `AsyncRecorder` is a simple queue with no batcher, WAL, or table routing.
- `main.py` never wires recorder consumers, so no events reach ClickHouse.
- DDLs for fills/positions/risk tables exist but unused.

### 5. Backtest & Analytics Runtime
- No `ClickHouseReplayFeed`, `SimulatedBroker`, or `BacktestRunner` implementation.
- `backtest_runs` / `backtest_timeseries` schemas not added; no CLI/notebooks.

### 6. Observability Instrumentation
- Metrics exist but `/metrics` endpoint, log shipping, alert rules, and dashboards are not wired into runtime.
- Recorder health metrics (queue usage, WAL size) and ClickHouse probes missing.

### 7. Rust Feature Engine Integration
- `rust_core.py` is a Python fallback; there is no `pyo3` module exposing Rust LOB/feature pipelines.
- No API for strategies to request Rust-computed features.

### 8. User Guide & Strategy Onboarding
- Lacks documentation guiding users through writing a strategy, running backtests, and deploying live.
- No reference strategies or CLI to scaffold a strategy module.

## Goals
1. Deliver a fully wired Online path: strategy contexts include real data, OrderAdapter/Execution/Risk/Recorder form a closed loop.
2. Persist all critical events to ClickHouse with WAL protection.
3. Provide a backtest runtime that reuses live logic and records results.
4. Expose Rust-accelerated feature interfaces while keeping Python strategy authoring simple.
5. Ship user-facing docs (guide + examples) to onboard strategy developers.
6. Instrument observability end-to-end (metrics endpoint, alerts, dashboards).
