# Module Documentation Index

Each file below documents a Python subpackage under `src/hft_platform/`.

> **For code-level gotchas and patterns**, see `.agent/memory/module_gotchas.md`.
> **For the full directory tree**, see `.agent/memory/codebase_map.md`.

## Hot Path Modules (Latency-Critical)

| Module         | Doc                                | Key Classes                                          |
| -------------- | ---------------------------------- | ---------------------------------------------------- |
| `feed_adapter` | [feed_adapter.md](feed_adapter.md) | `ShioajiClient`, `MarketDataNormalizer`, `LOBEngine` |
| `engine`       | [engine.md](engine.md)             | `RingBufferBus`                                      |
| `strategy`     | [strategy.md](strategy.md)         | `BaseStrategy`, `StrategyContext`, `StrategyRunner`  |
| `risk`         | [risk.md](risk.md)                 | `RiskEngine`, `StormGuardFSM`                        |
| `order`        | [order.md](order.md)               | `OrderAdapter`                                       |
| `gateway`      | [gateway.md](gateway.md)           | `GatewayService` (CE-M2)                             |
| `execution`    | [execution.md](execution.md)       | `ExecutionRouter`, `PositionStore`                   |

## Data & Events

| Module      | Doc                          | Key Classes                                                 |
| ----------- | ---------------------------- | ----------------------------------------------------------- |
| `events`    | [events.md](events.md)       | `TickEvent`, `BidAskEvent`, `LOBStatsEvent`                 |
| `contracts` | [contracts.md](contracts.md) | `OrderIntent`, `OrderCommand`, `FillEvent`, `PositionDelta` |
| `core`      | [core.md](core.md)           | `timebase`, `PriceCodec`, `market_calendar`                 |
| `features`  | [features.md](features.md)   | `OFI`, `MicroPrice`, `Entropy`, `Fractal`                   |

## Infrastructure

| Module          | Doc                                  | Key Classes                                                  |
| --------------- | ------------------------------------ | ------------------------------------------------------------ |
| `recorder`      | [recorder.md](recorder.md)           | `RecorderService`, `Batcher`, `DataWriter`, `WALFirstWriter` |
| `services`      | [services.md](services.md)           | `HFTSystem`, `SystemBootstrapper`, `MarketDataService`       |
| `config`        | [config.md](config.md)               | `load_settings()`                                            |
| `observability` | [observability.md](observability.md) | `MetricsRegistry`, `LatencyRecorder`                         |
| `schemas`       | [schemas.md](schemas.md)             | ClickHouse DDL                                               |
| `ipc`           | [ipc.md](ipc.md)                     | `ShmRingBuffer`                                              |
| `utils`         | [utils.md](utils.md)                 | Logging, metrics helpers                                     |

## Application Layer

| Module       | Doc                            | Key Classes           |
| ------------ | ------------------------------ | --------------------- |
| `cli`        | [cli.md](cli.md)               | `main()`, subcommands |
| `main`       | [main.md](main.md)             | Entry points          |
| `strategies` | [strategies.md](strategies.md) | Built-in strategies   |
| `backtest`   | [backtest.md](backtest.md)     | Backtest adapter      |
