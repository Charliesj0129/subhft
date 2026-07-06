# Modules Reference (Consolidated)

> **For code-level gotchas and patterns**, see `.agent/memory/module_gotchas.md`.
> **For canonical architecture**, see `docs/architecture/current-architecture.md`.

This document is a compressed directory map of `src/hft_platform/` (23 top-level packages, 30 incl. nested, 372 Python files).

<!-- Directory map from codebase scan 2026-04-01; counts re-verified 2026-07-06 (manual doc — no generator script). Descriptions not re-verified. -->

## Hot Path Modules (Latency-Critical)

These modules run the primary event loop. Any changes here must adhere strictly to the **Precision Law** (no floats for prices) and **Allocator Law** (no heap allocations per tick).

| Subpackage | Key Classes / Files | Responsibility |
|---|---|---|
| **feed_adapter/** | `Normalizer` (51KB), `LOBEngine` (27KB), `broker_registry`, `protocol` | Multi-broker ingestion via registry pattern (`HFT_BROKER`). Normalizes to `TickEvent`/`BidAskEvent`. Maintains per-symbol LOB state. Emits `LOBStatsEvent`. Extensive Rust fast-paths. |
| **feed_adapter/_base/** | `BaseBrokerSessionRuntime`, `BaseQuoteWatchdog`, `CooldownManager` | Shared broker abstractions: login retry + exponential backoff, feed stall watchdog, subscription cooldown. |
| **feed_adapter/shioaji/** | `ShioajiClientFacade`, `ShioajiClient` (44KB), `TickDispatcher`, `ReconnectOrchestrator` + 16 more | **26 files.** Full Shioaji sub-package: session/quote/order/account/contracts runtimes, tick dispatch (async worker thread), reconnect orchestration, multi-client routing, WebSocket connection pool. |
| **feed_adapter/fubon/** | `FubonClientFacade`, `FubonQuoteRuntime` + 12 more | **14 files.** Fubon sub-package (same structure as Shioaji). Pre-allocated translation buffers, 10s cooldown. |
| **feature/** | `FeatureEngine` (38KB), `FeatureRegistry`, `LobFeatureKernelV1`, `BurstDetector` | 27 LOB-derived features (v3: stateless + rolling + depth/toxicity + multi-window EMA). Default: `lob_shared_v3`. Burst detection. Python/Rust dual kernel. `HFT_FEATURE_ENGINE_ENABLED=1`. |
| **engine/** | `RingBufferBus` | Event bus: 3 modes (python deque / rust_pyobj / rust_typed). Lock-free ring buffer pub/sub. Routes events to StrategyRunner. |
| **strategy/** | `StrategyRunner`, `BaseStrategy`, `StrategyContext`, `StrategyRegistry` | Consumes bus events, dispatches to strategies, collects `OrderIntent`. Per-strategy circuit breaker (Rust accel). Budget timeout. Feature compat checking. |
| **risk/** | `RiskEngine` (25KB), `StormGuard`, 5 validators, `FastGate`, `GreeksLimitValidator`, `LiquidityGate` | **10 files.** Synchronous validation chain (PriceBand, MaxNotional, PositionLimit, DailyLoss, Greeks). StormGuard FSM (NORMAL→WARN→HALT). Rust FFI fast-path. Hot-reload via SIGHUP. |
| **order/** | `OrderAdapter` (53KB), `CircuitBreaker`, `DeadLetterQueue`, `HaltCanceller`, `ShadowOrderSink` | **7 files.** Order dispatch + lifecycle: rate limiting (180/250 per 10s), 5ms coalescing, per-strategy circuit breaker, shadow mode, DLQ (TTL 30s). |
| **execution/** | `ExecutionRouter`, `PositionStore`, `ExecutionNormalizer`, `ExecutionOptimizer`, `ImbalanceTimer`, `RegimeClassifier` | **11 files.** Fill normalization, integer-only position tracking (`RustPositionTracker`), startup/EOD reconciliation, checkpoint, limit-vs-market decision (Albers 2025), LOB imbalance timing (IC=+0.116), regime classification, slippage tracking, MTM, fill DLQ. |
| **gateway/** | `GatewayService`, `ExposureStore`, `IdempotencyStore`, `GatewayPolicy`, `LeaderLease` | **7 files.** CE-M2 optional gateway: 7-step synchronous dispatch (dedup, policy, exposure, risk, command, dispatch, commit). Bounded 10K entries with eviction. `HFT_GATEWAY_ENABLED=0` (default off). |

## Data & Event Models (Contracts)

| Subpackage | Key Classes / Files | Responsibility |
|---|---|---|
| **events.py** | `TickEvent`, `BidAskEvent`, `LOBStatsEvent`, `FeatureUpdateEvent`, `BookStats`, `FusedBookStats` | Canonical event types on the EventBus. All prices **scaled `int` (x10000)**. |
| **contracts/** | `OrderIntent`, `OrderCommand`, `RiskDecision`, `FillEvent`, `PositionDelta`, `OrderEvent` | Inter-module boundaries (strategy to risk to execution). `types.py`: `Side`, shared enums. |
| **trade_classifier.py** | `TradeClassifier` | EMO trade classification: AT_QUOTE(1000), INSIDE(800), TICK_RULE(500). |

## Infrastructure & Control Plane

| Subpackage | Key Classes / Files | Responsibility |
|---|---|---|
| **services/** | `HFTSystem`, `SystemBootstrapper`, `MarketDataService` (5 mixins), `ServiceRegistry`, `HeartbeatService` | **13 files.** App lifecycle: builds 5 bounded queues + 18 services. Supervision loop (lag/depth/liveness). SIGTERM/SIGINT handler. |
| **config/** | `loader.py`, `schema.py`, `symbols.py`, `hot_reload.py`, `wizard.py` | **10 files.** 5-layer config merge (Base YAML, Env YAML, settings.py, ENV, CLI). msgspec validation. Symbol DSL. Hot-reload strategy limits. |
| **core/** | `timebase`, `pricing.PriceCodec`, `order_ids`, `instrument_registry`, `market_calendar`, `rate_limiter`, `secret_validator`, `session_hooks` | **9 files.** `now_ns()` (mandatory). PriceCodec (x10000 scaled int). Trading calendar. Rate limiter. |
| **recorder/** | `RecorderService`, `Batcher`, `DataWriter`, `WALWriter`, `WALLoaderService`, `DiskPressureMonitor` | **22 files.** Durable storage: columnar double-buffer batching, ClickHouse insert + WAL fallback, WAL-first mode (CE-M3), WAL replay with dedup/DLQ, disk pressure monitor, shard claim. |
| **migrations/** | `clickhouse/*.sql` (26 files, 1136 lines) | ClickHouse DDL management. Tables: market_data (6mo TTL), orders/fills (1yr), audit (2yr). Auto-applied on boot. |
| **observability/** | `MetricsRegistry` (100+ metrics), `HealthServer`, `LatencySpan` | **6 files.** Prometheus metrics. HTTP health endpoints (`/healthz`, `/readyz`, `/status`). Pipeline latency tracking. |
| **notifications/** | `NotificationDispatcher`, `TelegramSender`, `WebhookSender`, `AlertManagerBridge` | **10 files.** Critical (HALT, daily loss) + normal event routing. Telegram/Webhook/AlertManager. |
| **ipc/** | `ShmSnapshotTable` | Shared memory snapshot for inter-process state sharing. |

## Operations Plane

| Subpackage | Key Classes / Files | Responsibility |
|---|---|---|
| **ops/** | `SessionGovernor`, `TrackGate`, `AutonomyMonitor`, `PositionFlattener`, `MarginMonitor`, `BackupManager` | **19 files.** Session phase FSM (INIT to CLOSED). Autonomy degradation (NORMAL to HALT). Emergency flattening. Margin monitoring. Config snapshot. Daily PnL report. Manual rearm. |

## Application Layer

| Subpackage | Key Classes / Files | Responsibility |
|---|---|---|
| **cli/** | 13 subcommand modules: `_run`, `_alpha`, `_feature`, `_health`, `_ops`, `_risk`, `_symbols`, `_tca`, `_checks`, `_feasibility`, `_golive` | CLI entry point (`hft`). Commands: run, init, check, wizard, alpha, feature, config, backtest, recorder, diag, feed. |
| **strategies/** | `simple_mm`, `mm_hawkes`, `cascade_bounce`, `opportunistic_mm`, `electronic_eye`, `vpin_regime_switch`, `rust_alpha` | **7 core strategies.** Plus 5 alpha strategies in `alpha/` subdir: `alpha_ofi`, `alpha_hawkes`, `alpha_deep_hawkes`, `alpha_mhp`, `alpha_propagator`. |
| **alpha/** | `validation.py` (57KB), `promotion.py` (39KB), `canary.py`, `experiments.py`, `pool.py`, `paper_trade_runner.py`, `latency_audit.py`, `screener.py`, `audit.py` | **54 files.** 6-gate alpha governance: Gate A (data), B (statistical), C (backtest), D (quant threshold), E (paper-trade), F (Rust readiness). |
| **backtest/** | `BacktestRunner`, `BacktestAdapter`, `EquityTracker` | **13 files.** HftBacktest integration: JSONL to NPZ convert, feed/elapse loops, equity tracking, scorecard reporting. |
| **monitor/** | `MonitorEngine`, `MonitorRenderer`, `TUI`, `CHPoller`, `RedisPoller` | **19 files.** Live signal monitoring TUI. Dual data: ClickHouse (historical) + Redis (live). Panels: portfolio, PnL, orders, positions, health, Greeks. |
| **bot/** | `app.py`, `handlers.py`, `scheduler.py` | Telegram Bot: interactive commands, scheduled reports. |
| **reports/** | `ReportingPipeline`, `ReportCollector`, `ReportComposer`, `ReportDistributor`, `FactExtractor`, `ReportReasoner` | **17 files.** Daily market report pipeline: collect, extract facts, reason, compose, distribute. Rules: `informed_flow`, `scenario_rules`, `support_resistance`. |
| **tca/** | `SlippageAnalyzer`, `FeeCalculator`, `SlippageRecord` | **6 files.** Transaction Cost Analysis: decision-vs-fill slippage, maker/taker fees, reporting. |
| **options/** | `GreeksCalculator`, `OptionsPricer`, `VolatilitySurface`, `OptionsLiveAdapter` | **4 files.** Black-Scholes pricing, Greeks, IV surface, live option quote adapter. |
| **analytics/** | `queries.py` | Pre-built ClickHouse analytical queries. |
| **data_quality/** | `DataProfiler` | Data completeness/outlier/gap profiling. |
| **diagnostics/** | `replay.py`, `trace.py` | Event replay for post-mortem, decision trace sampling. |
| **testing/** | `LoadGenerator`, `FaultInjector`, `ShadowRunner` | Load testing, fault injection, shadow execution. |
| **utils/** | `logging.py`, `serialization.py` | structlog setup, JSON/orjson serialization helpers. |
| **scripts/** | 7 operational scripts | Synthetic data generation, latency monitoring, futures subscription. |

<!-- END AUTO-GENERATED -->

## Summary Statistics

| Metric | Count |
|--------|-------|
| Python packages | 23 top-level (30 incl. nested) |
| Python files | 372 |
| Rust exports | 36 pyclass + 22 pyfunction |
| Makefile targets | 139 |
| ClickHouse tables | 13+ |
| Prometheus metrics | 100+ |
| Environment variables | 60+ |
