<!-- Generated: 2026-03-30 | Files scanned: 312 | Token estimate: ~900 -->

# Architecture Codemap

## System Type
Event-driven HFT platform. Python 3.12 + Rust (PyO3) + ClickHouse + Prometheus.

## Runtime Pipeline (Hot Path)

```
Exchange
  → BrokerFacade (Shioaji|Fubon)     [feed_adapter/<broker>/quote_runtime.py]
    → raw_queue (bounded 8192)
      → Normalizer                    [feed_adapter/normalizer.py, Rust optional]
        → LOBEngine                   [feed_adapter/lob_engine.py]
          → FeatureEngine (27 feat)   [feature/engine.py]
            → RingBufferBus           [engine/event_bus.py, Rust lock-free]
              → StrategyRunner        [strategy/runner.py]
                → OrderIntent
                  → risk_queue (4096)
                    → RiskEngine      [risk/engine.py]
                      → OrderCommand
                        → order_queue (2048)
                          → OrderAdapter  [order/adapter.py]
                            → BrokerFacade.place_order()
```

## Recording Path (Parallel, Non-Blocking)

```
MarketDataService → recorder_queue (16384, drop-on-full)
  → RecorderService → Batcher → ClickHouse INSERT
                              → WAL fallback (if wal_first mode)
```

## Execution Path (Separate Thread)

```
Broker callbacks → raw_exec_queue (4096)
  → ExecutionRouter → ExecutionNormalizer → PositionStore
                    → FillEvent/PositionDelta → bus
```

## Runtime Planes (7)

| Plane         | Key Modules                              | LOC    |
|---------------|------------------------------------------|--------|
| Control       | services/bootstrap, services/system      | 5,269  |
| Market Data   | feed_adapter/ (normalizer, lob_engine)   | 11,915 |
| Feature       | feature/ (engine, registry, burst)       | 2,355  |
| Decision      | strategy/, strategies/, risk/            | 6,378  |
| Execution     | order/, execution/                       | 4,378  |
| Persistence   | recorder/                                | 6,336  |
| Observability | observability/, risk/storm_guard         | ~1,469 |

## Package Dependency Layers

```
L0: core, contracts, events           (no internal deps)
L1: feed_adapter, observability, engine
L2: feature, gateway, recorder
L3: order, execution, risk
L4: strategy, strategies, alpha
L5: services (orchestrates all)
L6: reports, bot, monitor, ops        (application layer)
```

## Entry Points

| Command              | Handler                    | Purpose                  |
|----------------------|----------------------------|--------------------------|
| `hft run sim\|live`  | cli.cmd_run → HFTSystem    | Main trading runtime     |
| `hft alpha *`        | cli → alpha/               | Alpha governance (A-E)   |
| `hft feature *`      | cli → feature/             | Feature rollout mgmt     |
| `hft backtest`       | cli → backtest/            | Research backtest         |
| `hft monitor`        | cli → monitor/             | Live TUI                 |

## Key Env Vars (Architecture-Affecting)

| Variable                      | Effect                                        |
|-------------------------------|-----------------------------------------------|
| `HFT_MODE=sim\|live\|replay` | Runtime mode                                  |
| `HFT_BROKER=shioaji\|fubon`  | Broker backend selection                      |
| `HFT_GATEWAY_ENABLED=1`      | Enable CE-M2 order/risk gateway               |
| `HFT_RECORDER_MODE=wal_first`| WAL-only persistence path                     |
| `HFT_FEATURE_ENGINE_ENABLED` | Feature plane (default on, v3 with 27 feats)  |
| `HFT_FUSED_NORMALIZER=1`     | Rust fused normalizer+LOB pipeline            |
