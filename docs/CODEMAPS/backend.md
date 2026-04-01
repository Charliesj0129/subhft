<!-- Generated: 2026-03-30 | Files scanned: 312 | Token estimate: ~950 -->

# Backend Codemap

## Service Graph (bootstrap.py → system.py)

```
SystemBootstrapper.build()
  ├─ Queues: raw(65536), raw_exec(8192), risk(4096), order(2048), recorder(16384)
  ├─ RingBufferBus (Rust lock-free)
  ├─ MarketDataService ← raw_queue consumer, bus+recorder publisher
  ├─ FeatureEngine (v3, 27 features) ← LOBEngine output, FeatureUpdateEvent publisher
  ├─ StrategyRunner ← bus consumer → OrderIntent → risk_queue
  ├─ RiskEngine|GatewayService ← risk_queue → OrderCommand → order_queue
  ├─ OrderAdapter ← order_queue → broker API
  ├─ ExecutionRouter ← raw_exec_queue → fills/positions → bus
  ├─ ReconciliationService ← bus consumer (fill reconciliation)
  ├─ RecorderService ← recorder_queue → ClickHouse/WAL
  └─ Optional: SessionGovernor, AutonomyMonitor, CheckpointService
```

## Module Map (src/hft_platform/)

### Hot Path (latency-critical)

| Module | Files | LOC | Key Classes | Responsibility |
|--------|-------|-----|-------------|----------------|
| feed_adapter/ | 46 | 11,915 | BrokerFacade, Normalizer, LOBEngine | Multi-broker ingestion, normalize, LOB |
| feed_adapter/shioaji/ | 20 | ~5,800 | ShioajiFacade, QuoteRuntime, TickDispatcher, ReconnectOrch | Shioaji broker integration |
| feed_adapter/fubon/ | 14 | ~2,000 | FubonFacade, QuoteRuntime | Fubon broker integration |
| engine/ | 1 | 710 | RingBufferBus | Lock-free event routing |
| feature/ | 9 | 2,355 | FeatureEngine, FeatureRegistry, BurstDetector | 27 LOB-derived features (v3) |
| strategy/ | 4 | 1,368 | StrategyRunner, BaseStrategy, StrategyContext | Strategy SDK + dispatch |
| risk/ | 8 | 2,357 | RiskEngine, StormGuardFSM | Risk validation + HALT FSM |
| order/ | 6 | 1,643 | OrderAdapter, CircuitBreaker | Broker dispatch + rate limits |
| execution/ | 15 | 2,735 | ExecutionRouter, PositionStore, ExecutionOptimizer, ImbalanceTimer, RegimeClassifier | Fill routing, position O(1), smart execution, regime detection |

### Infrastructure

| Module | Files | LOC | Key Classes | Responsibility |
|--------|-------|-----|-------------|----------------|
| services/ | 11 | 5,269 | HFTSystem, SystemBootstrapper, MarketDataService | Lifecycle + orchestration |
| config/ | 10 | 2,425 | ConfigLoader, SymbolManager | Layered config merge |
| core/ | 9 | 1,358 | InstrumentRegistry, RateLimiter, Timebase | Shared primitives |
| recorder/ | 22 | 6,336 | RecorderService, WALWriter, DataWriter | ClickHouse + WAL persistence |
| observability/ | 5 | 1,469 | MetricsRegistry, LatencyRecorder | Prometheus metrics |
| gateway/ | 7 | 1,654 | GatewayService, ExposureStore | CE-M2 order gateway (opt-in) |
| notifications/ | 6 | 1,512 | TelegramNotifier, AlertmanagerBridge, Dispatcher | Multi-channel alerts |

### Application

| Module | Files | LOC | Key Classes | Responsibility |
|--------|-------|-----|-------------|----------------|
| strategies/ | 7 | 2,653 | CascadeBounce, OpportunisticMM, SimpleMM | Strategy implementations |
| alpha/ | 25 | 7,912 | AlphaValidator, PromotionEngine, CanaryManager | Alpha governance (Gate A-E) |
| reports/ | 10 | 4,121 | FactExtractor, Reasoner, Composer, Distributor | Three-layer market analysis |
| monitor/ | 19 | 5,599 | MonitorEngine, Renderer | Live TUI monitoring |
| bot/ | 5 | 522 | TelegramBotService, Handlers, Scheduler | Interactive Telegram bot |
| backtest/ | 11 | 1,601 | HftBacktestAdapter, BacktestRunner | Research backtest integration |
| cli/ | 15 | 3,133 | main(), cmd_run, cmd_alpha | CLI entry point |
| ops/ | 15 | 2,689 | BackupManager, DeployGuard | Ops tooling |

## Strategy Implementations

| Strategy | File | Purpose | Status |
|----------|------|---------|--------|
| SimpleMM | simple_mm.py | Basic market-making | Active |
| OpportunisticMM | opportunistic_mm.py | Reactive MM with spread gate + toxicity filter | Shadow |
| CascadeBounce | cascade_bounce.py | Contrarian bounce after cascade move | Shadow (TMF) |
| ElectronicEye | electronic_eye.py | TXO options MM (Guardian/Quoter/Hedger) | Scaffold |
| RustAlpha | rust_alpha.py | Rust-accelerated alpha executor | Active |
| VPINRegimeSwitch | vpin_regime_switch.py | VPIN-based regime detection | Research |
| MMHawkes | mm_hawkes.py | Hawkes-process market-making | Research |

## Feature Engine v3 (27 features)

```
v1 [0-15]:  best_bid, best_ask, mid_price_x2, spread_scaled, bid/ask_depth,
            depth_imbalance_ppm, microprice_x2, l1_bid/ask_qty, l1_imbalance_ppm,
            ofi_l1_raw/cum/ema8, spread_ema8_scaled, depth_imbalance_ema8_ppm

v2 [16-21]: ofi_depth_norm_ppm, ret_autocov_5s_x1e6, tob_survival_ms,
            impact_surprise_x1000, deep_depth_momentum_x1000, toxicity_ema50_x1000

v3 [22-26]: ofi_l1_ema5s, ofi_l1_ema30s, imbalance_ema5s_ppm,
            spread_ema30s, spread_ema300s
```

## Reports Pipeline (NEW three-layer architecture)

```
DataCollector → FactExtractor (6 extractors)
  → Reasoner (bias, levels, scenarios, narrative)
    → Composer (tier-aware MessageParts)
      → Distributor (Telegram, file)
```

## Execution Middleware

```
OrderIntent
  → ExecutionOptimizer.decide() → LIMIT|MARKET (Albers 2025, fill probability)
  → ImbalanceTimer.should_execute() → wait for favorable LOB imbalance
  → OrderAdapter.place_order()
```

## Queue Overflow Policy

| Queue | Size | Overflow Action |
|-------|------|-----------------|
| raw_queue | 65536 | Drop + degrade mode |
| raw_exec_queue | 8192 | Overflow ring buffer (4096) → 3+ overflows = HALT |
| risk_queue | 4096 | Backpressure |
| order_queue | 2048 | Backpressure |
| recorder_queue | 16384 | Drop + degraded mode (never block hot path) |
