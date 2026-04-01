---
name: hft-architect
description: Use when designing or reviewing platform architecture, changing runtime boundaries, introducing new hot-path stages, or evaluating Python-Rust, broker, recorder, or feature-plane design decisions.
---

# HFT Architecture

Use this skill when a change affects module boundaries, runtime flow, hot-path cost, or the Python-Rust split. Canonical baseline: `docs/architecture/current-architecture.md` and `.agent/rules/`.

## Runtime Planes (7)

| Plane | Key Modules | Files | Responsibility |
| --- | --- | --- | --- |
| **Control** | services/bootstrap, services/system, config/ | 31 | Queues, service graph, supervision, HALT enforcement |
| **Market Data** | feed_adapter/ (shioaji 20 + fubon 14 + base 3 + root 7), normalizer, lob_engine | 47 | Ingest, normalize, LOB state, Rust fast-paths |
| **Feature** | feature/engine, registry, kernel, burst_detector | 8 | 27 features (v3), Python/Rust dual kernel |
| **Decision** | strategy/runner, risk/engine, risk/storm_guard | 14+10 | Strategy dispatch, risk validation, StormGuard FSM |
| **Execution** | order/adapter, execution/router, execution/positions | 6+15 | Order dispatch, fill routing, position O(1), reconciliation |
| **Persistence** | recorder/ (worker, batcher, writer, WAL, loader) | 22 | ClickHouse + WAL, dual mode, disk monitor, shard claim |
| **Observability** | observability/metrics, notifications/dispatcher | 5+5 | 100+ Prometheus metrics, Telegram/Webhook alerts |

**Ops layer** (cross-cutting): `ops/` (14 files) — session governor, autonomy degradation, position flattener, margin monitor, backup.

Keep new work inside one plane when possible. Escalate when a change crosses planes.

## Canonical Runtime Flow

```text
market callback -> raw_queue(65536) -> normalize/LOB -> FeatureEngine(27) -> RingBufferBus
    -> StrategyRunner -> OrderIntent -> risk_queue(4096)
        -> RiskEngine (validators + StormGuard) -> OrderCommand -> order_queue(2048)
            -> OrderAdapter (rate limit, circuit breaker, coalesce)
                -> BrokerFacade.place_order()

broker callbacks -> raw_exec_queue(8192)
    -> ExecutionRouter -> ExecutionNormalizer -> PositionStore (integer-only)
        -> [PositionDelta, FillEvent] -> bus + recorder_queue(16384)

recorder_queue -> RecorderService -> Batcher (columnar double-buffer)
    -> DataWriter -> ClickHouse INSERT | WAL fallback
```

## Execution Middleware (NEW)

Between strategy intent and broker submission:

```text
OrderIntent
  -> ExecutionOptimizer.decide() -> LIMIT|MARKET (fill probability from LOB queue depth)
  -> ImbalanceTimer.should_execute() -> delay until favorable LOB imbalance
  -> RegimeClassifier.classify() -> FAVORABLE|NEUTRAL|ADVERSE
  -> OrderAdapter.place_order()
```

Post-fill:
```text
FillEvent -> PositionStore (Rust O(1)) -> Reconciliation (startup + EOD + periodic)
  -> Checkpoint (periodic snapshot) -> MTM (mark-to-market)
  -> SlippageTracker -> TCA (decision_price vs fill_price)
```

## Operations Layer (NEW)

```text
SessionGovernor: INIT -> PRE_OPEN -> OPEN -> CLOSE_ONLY -> FORCE_FLAT -> CLOSED
  -> TrackGate (O(1) per-symbol phase lookup)

AutonomyMonitor: checks every 100ms-1s
  -> CH stale > 60s | feed gap > 50% | queue > 90% | RSS > threshold | PnL drawdown
  -> AutonomyMode: NORMAL -> PLATFORM_REDUCE_ONLY -> HALT
  -> PositionFlattener (emergency close, 120s deadline)
  -> manual_rearm() to recover
```

## Design Review Checklist

1. Will the change allocate on the hot path? (Allocator Law)
2. What latency budget does it consume? (< 1ms rule)
3. Does it block the event loop or quote callback path? (Async Law)
4. Does the data layout preserve locality and low-copy boundaries? (Cache Law)
5. How does the system degrade or recover when the component fails?
6. Does it introduce unbounded state? (ExposureStore Rule: max 10K entries)
7. Does it cross Python-Rust boundary with copies? (Boundary Law)

## High-Risk Areas

- **Broker adapters**: Protocol boundaries, SDK import guards, no broker leakage
- **Recorder durability**: WAL replay compatibility, schema evolution, dedup
- **Feature-plane parity**: 27 features must match across research/replay/live
- **Python-Rust interfaces**: Hidden fallbacks, copy overhead, panic risk
- **Unbounded maps**: order_id_map (10K FIFO), ExposureStore (10K eviction), metrics labels (200 cap)
- **Execution middleware**: Optimizer/ImbalanceTimer state, regime classifier accuracy
- **Ops safety**: SessionGovernor phase transitions, autonomy HALT->NORMAL requires manual rearm

## Boundary Rules

- `contracts/` must NOT import runtime services
- `events.py` must NOT import strategy or execution
- broker-specific logic stays inside `feed_adapter/<broker>/`
- recorder writes must be durability-safe under ClickHouse failure
- pricing/accounting fields use scaled-int on live paths (Precision Law)
- new hot-path component requires 5-gate design review in `.agent/library/design-review-artifacts.md`

## Build and Verification

```bash
uv run maturin develop --manifest-path rust_core/Cargo.toml  # Rebuild Rust
make ci                                                        # Full quality gate
make hotpath-profile                                           # Latency regression check
```

Update when responsibilities move:
- `docs/architecture/current-architecture.md` (canonical)
- `docs/MODULES_REFERENCE.md` (37 packages)
- `docs/CODEMAPS/` (architecture, backend, data, dependencies)
