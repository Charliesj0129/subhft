# HFT Platform Current Architecture Baseline

Date: 2026-02-21
Scope: As-built implementation under `src/hft_platform`, `research`, `rust_core`, `rust`, `config`, and `docker-compose.yml`.
Companion target document: `.agent/library/target-architecture.md`.
Companion C4 diagrams: `.agent/library/c4-model-current.md`.
Companion research execution plan: `.agent/library/research_pipeline_execution_plan.md`.
Companion Rust boundary note: `.agent/library/rust_pyo3.md`.
Companion planned feature unification spec (TODO): `docs/architecture/feature-engine-lob-research-unification-spec.md`.
Companion cluster backlog: `.agent/library/cluster-evolution-backlog.md`.

## 1. Architecture Slices (As-Built)

1. Runtime trading system (live/sim)
- Entrypoint: `hft run sim|live` in `src/hft_platform/cli.py`.
- Runtime assembly: `HFTSystem` + `SystemBootstrapper` in `src/hft_platform/services/system.py` and `src/hft_platform/services/bootstrap.py`.
- Core loop: market data -> strategy -> risk -> order -> execution -> recorder.

2. Research and alpha governance system
- Entrypoints: `hft alpha *` commands in `src/hft_platform/cli.py`.
- Research packages: `research/registry`, `research/backtest`, `research/combinatorial`, `research/rl`.
- Promotion/governance runtime: `src/hft_platform/alpha/*` (validation, experiments, pool, promotion, canary, audit).

3. Shared acceleration and infra
- Rust extension: `rust_core` (PyO3) consumed by runtime hot paths.
- Optional Rust strategy crate: `rust` (`rust_strategy`).
- Storage/ops: ClickHouse, WAL, Prometheus/Grafana/Alertmanager, optional Redis.

## 2. Runtime Trading Planes

1. Control plane
- `src/hft_platform/cli.py` resolves settings and launches runtime.
- `src/hft_platform/config/loader.py` merges base/env/settings/env vars/CLI overrides.
- `src/hft_platform/services/bootstrap.py` creates bounded queues and service graph.

2. Market data plane
- `src/hft_platform/feed_adapter/shioaji_client.py`: login, contracts, quote callbacks, watchdog, reconnect, API caches.
- `src/hft_platform/services/market_data.py`: normalize payloads, update LOB, publish to bus, direct recorder mapping.
- `src/hft_platform/feed_adapter/normalizer.py`: raw payload -> normalized events (Python/Rust paths).
- `src/hft_platform/feed_adapter/lob_engine.py`: per-symbol LOB + stats.
- 🔄 TODO (planned): add a separate `FeatureEngine` / Feature Plane after `LOBEngine` for shared LOB-derived feature kernels (research/backtest/live parity). See `docs/architecture/feature-engine-lob-research-unification-spec.md`.

3. Decision plane
- `src/hft_platform/strategy/runner.py`: consumes bus events, executes strategies, emits `OrderIntent`.
- `src/hft_platform/risk/engine.py`: validators + StormGuard FSM, emits `OrderCommand`.

4. Execution plane
- `src/hft_platform/order/adapter.py`: API queue/coalescing, rate limits, circuit breaker, DLQ.
- `src/hft_platform/execution/gateway.py`: execution wrapper and liveness/error metrics.
- `src/hft_platform/execution/router.py`: normalizes order/deal callbacks, updates position store, republishes events.
- `src/hft_platform/execution/positions.py`: position accounting (Python + optional Rust tracker).
- `src/hft_platform/execution/reconciliation.py`: broker/local reconciliation, can trigger HALT.

5. Persistence plane
- `src/hft_platform/recorder/worker.py`: topic routing to table batchers.
- `src/hft_platform/recorder/batcher.py`: batching, flush policy, backpressure/memory guard.
- `src/hft_platform/recorder/writer.py`: ClickHouse insert path + WAL fallback.
- `src/hft_platform/recorder/wal.py`: WAL writer and batch writer.
- `src/hft_platform/recorder/loader.py`: WAL replay and retry backoff.

6. Observability and safety plane
- `src/hft_platform/observability/metrics.py` and `src/hft_platform/observability/latency.py`.
- `src/hft_platform/risk/storm_guard.py`: safety state machine.
- `src/hft_platform/services/system.py::_supervise()`: task supervision, loop lag, HALT enforcement.

## 3. Runtime Canonical Flow

1. `hft run sim|live` -> `cli.cmd_run()` -> `HFTSystem.run()`.
2. `SystemBootstrapper.build()` creates:
- `RingBufferBus`
- bounded queues: `raw_queue`, `raw_exec_queue`, `risk_queue`, `order_queue`, `recorder_queue`
- services: `MarketDataService`, `StrategyRunner`, `RiskEngine`, `OrderAdapter`, `ExecutionGateway`, `ExecutionRouter`, `ReconciliationService`, `RecorderService`
3. Market data:
- Shioaji callback -> `raw_queue`
- `MarketDataService`: normalize -> LOB update -> publish events to bus
- optional direct recorder mapping to `recorder_queue`
 - 🔄 TODO (planned): `LOBEngine -> FeatureEngine -> StrategyRunner` path for shared microstructure features (feature plane not implemented in as-built baseline)
4. Strategy:
- `StrategyRunner` consumes bus events -> `OrderIntent` -> `risk_queue`
5. Risk:
- `RiskEngine` validates -> `OrderCommand` -> `order_queue`
6. Order and execution:
- `OrderAdapter` dispatches to broker API
- broker callbacks -> `raw_exec_queue`
- `ExecutionRouter` emits normalized execution events + position deltas
7. Recorder:
- `RecorderService` drains `recorder_queue` -> ClickHouse
- on failure, fallback to WAL; replay via `WALLoaderService`
8. Supervision:
- loop lag, queue depth, service liveness, StormGuard HALT gating

## 4. Research and Alpha Governance Flow (As-Built)

1. Discovery and scaffolding
- `hft alpha scaffold` calls `research/tools/alpha_scaffold.py`.
- `hft alpha list` loads artifacts through `research/registry/alpha_registry.py`.

2. Validation (Gate A-C)
- `hft alpha validate` calls `src/hft_platform/alpha/validation.py`.
- Gate A: manifest/data-field/complexity checks.
- Gate B: per-alpha pytest execution.
- Gate C: standardized research backtest (`research/backtest/hbt_runner.py`) + scorecard write (`research/registry/scorecard.py`) + experiment logging (`src/hft_platform/alpha/experiments.py`).

3. Pool and search
- `hft alpha search`: combinatorial expression search (`research/combinatorial/search_engine.py`).
- `hft alpha pool`: correlation matrix, redundancy detection, weight optimization, marginal contribution test (`src/hft_platform/alpha/pool.py`).

4. Promotion (Gate D-E)
- `hft alpha promote` calls `src/hft_platform/alpha/promotion.py`.
- Gate D: scorecard thresholds.
- Gate E: shadow session and execution quality thresholds.
- On approval: writes canary config to `config/strategy_promotions/YYYYMMDD/<alpha_id>.yaml`.

5. Canary lifecycle
- `hft alpha canary status/evaluate` uses `src/hft_platform/alpha/canary.py`.
- Supports hold/escalate/rollback/graduate decisions.

6. Experiment and RL integration
- `hft alpha experiments list|compare|best` reads `research/experiments/runs/*/meta.json`.
- RL bridge in `research/rl/lifecycle.py` logs RL runs and can promote latest run via Gate D-E (`hft alpha rl-promote`).

## 4A. Planned Research/Runtime Feature Unification (TODO, Not Yet Implemented)

Reference spec: `docs/architecture/feature-engine-lob-research-unification-spec.md`.

Planned direction:
1. Introduce a Feature Plane (`FeatureEngine`) after `LOBEngine` in runtime and replay/backtest paths.
2. Add `hftbacktest` adapter feature mode so backtests can use the same shared feature kernels as live.
3. Migrate shared microstructure features out of strategies and into governed feature kernels.
4. Preserve strategy-level decision logic in strategy modules (feature plane computes shared inputs only).

Status:
- 🔄 TODO `FeatureEngine` runtime component
- 🔄 TODO `HftBacktestAdapter` feature-first mode (`lob_feature`)
- 🔄 TODO Feature ABI/versioning/parity gates (Python reference vs Rust kernels)

## 5. Module Inventory (Current)

| Domain | Current functionality | Key files |
|---|---|---|
| `cli` | runtime, backtest, alpha, symbols and diagnostics commands | `src/hft_platform/cli.py` |
| `config` | layered settings and symbol DSL/sync | `src/hft_platform/config/loader.py`, `src/hft_platform/config/symbols.py` |
| `services` | runtime assembly and supervision | `src/hft_platform/services/bootstrap.py`, `src/hft_platform/services/system.py` |
| `feed_adapter` | broker integration, normalization, LOB, subscription lifecycle | `src/hft_platform/feed_adapter/shioaji_client.py`, `normalizer.py`, `lob_engine.py` |
| `engine` | ring buffer bus with overflow safeguards | `src/hft_platform/engine/event_bus.py` |
| `strategy`/`strategies` | strategy SDK and implementations | `src/hft_platform/strategy/*.py`, `src/hft_platform/strategies/*.py` |
| `risk` | risk checks, fast gate, StormGuard | `src/hft_platform/risk/*.py` |
| `order` | broker dispatch and order-path guardrails | `src/hft_platform/order/*.py` |
| `execution` | execution normalization, routing, reconciliation, position store | `src/hft_platform/execution/*.py` |
| `recorder` | recorder batching, writer, WAL, replay; WAL-first mode (CE-M3) | `src/hft_platform/recorder/*.py`; `wal_first.py`, `disk_monitor.py`, `mode.py`, `shard_claim.py`, `replay_contract.py` |
| `gateway` | order/risk gateway; ExposureStore, IdempotencyStore, GatewayPolicy (CE-M2, enabled via `HFT_GATEWAY_ENABLED=1`) | `src/hft_platform/gateway/` (channel, dedup, exposure, policy, service) |
| `observability` | Prometheus and latency spans | `src/hft_platform/observability/*.py` |
| `alpha` | validation (Gate A-C), promotion (Gate D-E), canary, audit, experiments, pool | `src/hft_platform/alpha/validation.py`, `promotion.py`, `canary.py`, `pool.py`, `experiments.py`, `audit.py` |
| `research.registry` | alpha schema/protocol, discovery, scorecards | `research/registry/*.py` |
| `research.backtest` | standardized research backtest and metrics | `research/backtest/*.py` |
| `research.combinatorial` | expression language + alpha search engine | `research/combinatorial/*.py` |
| `research.rl` | RL alpha adapter and lifecycle integration | `research/rl/*.py` |
| `backtest` | runtime backtest runner/adapter/reporting with real-equity-first extraction | `src/hft_platform/backtest/*.py` |

## 6. Rust Boundary (Current)

1. `rust_core` (PyO3, loaded as `hft_platform.rust_core` or `rust_core`)
- `FastRingBuffer`, `EventBus`
- book scaling/normalization helpers (`scale_book*`, `normalize_*`, `compute_book_stats`)
- `RustPositionTracker`
- `FastGate`
- alpha classes and `AlphaStrategy`
- shared memory ring buffer (`ShmRingBuffer`)

2. `rust_strategy` crate (`rust/`)
- exports `RLStrategy` and `RLParams`
- currently no active Python runtime callsite under `src/hft_platform/*`

3. Python call sites currently using Rust paths
- `src/hft_platform/feed_adapter/normalizer.py`
- `src/hft_platform/feed_adapter/lob_engine.py`
- `src/hft_platform/engine/event_bus.py`
- `src/hft_platform/execution/positions.py`
- `src/hft_platform/strategies/rust_alpha.py`

## 7. Persistence Surfaces (Current)

1. Runtime market/execution schema
- canonical DDL path: `src/hft_platform/schemas/clickhouse.sql`
- tables include `hft.market_data`, `hft.orders`, `hft.trades`, `hft.ohlcv_1m`, `hft.latency_stats_1m`, `hft.latency_spans`

2. Durability and replay
- fallback sink: `.wal/` via `WALWriter`
- wal_first mode: `src/hft_platform/recorder/wal_first.py` — WAL-only write path with per-topic disk pressure policy (CE-M3, enabled via `HFT_RECORDER_MODE=wal_first`)
- disk pressure monitor: `src/hft_platform/recorder/disk_monitor.py` — background daemon with OK/WARN/CRITICAL/HALT levels
- shard claim protocol: `src/hft_platform/recorder/shard_claim.py` — fcntl-based exclusive file ownership for multi-loader scale-out
- replay service: `src/hft_platform/recorder/loader.py`

3. Research artifacts
- alpha artifacts: `research/alphas/<alpha_id>/`
- experiment runs: `research/experiments/runs/<run_id>/meta.json`
- promotion configs: `config/strategy_promotions/YYYYMMDD/<alpha_id>.yaml`

4. Audit surfaces
- audit client writes in `src/hft_platform/alpha/audit.py` target `audit.alpha_*` tables.
- audit DDL exists in `src/hft_platform/schemas/audit.sql`.
- audit schema is not auto-applied by current `apply_schema()` path (manual bootstrap required).

## 8. Architectural Invariants (Unchanged)

1. Hot path must avoid blocking I/O and excessive allocation.
2. Accounting-critical price/balance/PnL paths should remain scaled-int or Decimal.
3. Bus/queue boundaries stay bounded and backpressure-aware.
4. Contract surfaces (`events.py`, `contracts/*`) are cross-module boundaries.
5. Recorder durability must preserve data under ClickHouse outages (WAL fallback).
6. HALT state must block new order progression.
7. 🔄 TODO (planned invariant for feature plane): shared promoted microstructure features must preserve parity across research replay, `hftbacktest`, and live runtime for the same feature set/version.

## 9. Observed Drift and Risks

1. ExposureStore symbol cardinality bound (HIGH — fix applied 2026-02-21)
- `ExposureStore._exposure` dict was unbounded; in production with many unique (account, strategy, symbol) tuples this was an OOM risk.
- Fix: `_max_symbols` bound (default 10,000, env `HFT_EXPOSURE_MAX_SYMBOLS`) with zero-balance eviction; `ExposureLimitError` on overflow.
- Tracked as CE2-12 in cluster-evolution-backlog.md.

2. Audit bootstrap gap (MEDIUM — D5 gap)
- alpha audit logs can be enabled, but audit tables (`src/hft_platform/schemas/audit.sql`) are not auto-initialized in runtime schema bootstrap path.
- Requires manual `apply_schema()` call or explicit deploy step. Fix tracked under M1.

3. Broker adapter decomposition not yet done (LOW — D2 pending)
- `ShioajiClient` still centralizes many concerns (session, quote stream, orders, account/cache).
- Decomposition into `session`, `contracts`, `quote_stream`, `order_gateway`, `account` submodules is backlogged as M2.

4. Schema duplication still present on disk
- runtime uses canonical `clickhouse.sql`, but legacy SQL files remain and can cause confusion without strict governance.

5. Research/live feature drift risk for shared microstructure factors (PLANNED mitigation, TODO)
- Current architecture allows equivalent features to be implemented separately in research and strategy/runtime code.
- Planned mitigation: Feature Plane + shared feature ABI/kernels (see `docs/architecture/feature-engine-lob-research-unification-spec.md`).

## 10. Cluster Evolution (Vector 2 and 3)

Status: CE-M2 and CE-M3 core modules implemented (2026-02-21). Hardening backlog open.
Detailed milestone/issue backlog: `.agent/library/cluster-evolution-backlog.md`.
C4 diagrams: `.agent/library/c4-model-current.md`.
Design review artifacts: `.agent/library/design-review-artifacts.md`.

### Vector 2 - Dedicated Order/Risk Gateway (CE-M2) — Core Implemented

Enabled via `HFT_GATEWAY_ENABLED=1`. Off by default for safe rollout.

| Issue | Title | Status |
|---|---|---|
| CE2-01 | Define gateway command envelope and idempotency contract | ✅ Implemented |
| CE2-02 | Implement distributed intent channel adapter (ack/retry semantics) | ✅ Implemented |
| CE2-03 | Create gateway service skeleton (`RiskEngine` + `OrderAdapter`) | ✅ Implemented |
| CE2-04 | Add global exposure state store and atomic check/update path | ✅ Implemented |
| CE2-05 | Implement command dedup and replay-safe processing | ✅ Implemented |
| CE2-06 | Add gateway fail-safe policy (reject/degrade/halt) and config flags | ✅ Implemented |
| CE2-10 | Isolate Shioaji callbacks to enqueue-only fast path | ✅ Implemented |
| CE2-12 | ExposureStore memory bound (symbol cardinality limit + eviction) | ✅ Implemented |
| CE2-07 | Add gateway metrics/alerts/dashboard and SLO definitions | 🔄 TODO |
| CE2-08 | Multi-runner integration test and chaos test for gateway outages | 🔄 TODO |
| CE2-09 | Add active/standby gateway failover and leader lease control | 🔄 TODO |
| CE2-11 | Enforce quote schema lock (`quote_version=v1`) with guardrails | 🔄 TODO |

### Vector 3 - Async WAL-First Cold Path (CE-M3) — Core Implemented

Enabled via `HFT_RECORDER_MODE=wal_first`. Shard claim via `HFT_WAL_SHARD_CLAIM_ENABLED=1`.

| Issue | Title | Status |
|---|---|---|
| CE3-01 | Add recorder mode switch (`direct` vs `wal_first`) and defaults | ✅ Implemented |
| CE3-02 | Implement strict WAL-first runtime path in RecorderService | ✅ Implemented |
| CE3-05 | Add WAL disk pressure controls and backpressure policy | ✅ Implemented |
| CE3-03 | Scale out WAL loader workers and shard assignment policy | 🔄 TODO |
| CE3-04 | Define replay safety contract (ordering + dedup + manifest) | 🔄 TODO |
| CE3-06 | Add WAL SLO metrics, alerts, and dashboards | 🔄 TODO |
| CE3-07 | Outage drills: ClickHouse down, slow, and WAL growth recovery | 🔄 TODO |
