# Module Documentation Index

> Per-module deep-dive documentation for `src/hft_platform/` (32 packages).
> For consolidated overview, see [MODULES_REFERENCE.md](../MODULES_REFERENCE.md).

## Hot-Path Modules (Latency-Critical)

| Module | Doc | Responsibility |
|--------|-----|----------------|
| `feed_adapter/` | [feed_adapter.md](feed_adapter.md) | Multi-broker ingestion, normalization, LOB |
| `feature/` | [feature.md](feature.md) | 27 LOB-derived features (v3), burst detection |
| `engine/` | [engine.md](engine.md) | RingBufferBus event routing (Disruptor pattern) |
| `strategy/` | [strategy.md](strategy.md) | Strategy SDK, runner, circuit breaker |
| `risk/` | [risk.md](risk.md) | Validator chain, StormGuard FSM |
| `order/` | [order.md](order.md) | Order dispatch, rate limiting, coalescing |
| `execution/` | [execution.md](execution.md) | Fill processing, position tracking, reconciliation |
| `gateway/` | [gateway.md](gateway.md) | CE-M2 intent routing (optional) |

## Data & Event Models

| Module | Doc | Responsibility |
|--------|-----|----------------|
| `events.py` | [events.md](events.md) | TickEvent, BidAskEvent, LOBStatsEvent, etc. |
| `contracts/` | [contracts.md](contracts.md) | OrderIntent, FillEvent, PositionDelta |
| `core/` | [core.md](core.md) | Timebase, pricing, instrument registry |

## Infrastructure & Control

| Module | Doc | Responsibility |
|--------|-----|----------------|
| `services/` | [services.md](services.md) | Runtime assembly, supervision |
| `config/` | [config.md](config.md) | 5-layer config, hot-reload |
| `recorder/` | [recorder.md](recorder.md) | ClickHouse + WAL persistence |
| `migrations/` | [migrations.md](migrations.md) | ClickHouse DDL management |

## Observability & Operations

| Module | Doc | Responsibility |
|--------|-----|----------------|
| `observability/` | [observability.md](observability.md) | Prometheus metrics, health server |
| `notifications/` | [notifications.md](notifications.md) | Telegram, webhook, AlertManager |
| `ops/` | [ops.md](ops.md) | Session governor, autonomy, flattening |
| `ipc/` | [ipc.md](ipc.md) | Shared memory snapshot table |

## Application Layer

| Module | Doc | Responsibility |
|--------|-----|----------------|
| `cli/` | [cli.md](cli.md) | CLI entry point (13 subcommands) |
| `strategies/` | [strategies.md](strategies.md) | 7 core + 5 alpha strategy implementations |
| `alpha/` | [alpha.md](alpha.md) | 6-gate alpha governance pipeline |
| `backtest/` | [backtest.md](backtest.md) | HftBacktest integration |
| `monitor/` | [monitor.md](monitor.md) | Live signal monitoring TUI |
| `bot/` | [bot.md](bot.md) | Telegram bot |
| `reports/` | [reports.md](reports.md) | Daily market report pipeline |

## Auxiliary

| Module | Doc | Responsibility |
|--------|-----|----------------|
| `tca/` | [tca.md](tca.md) | Transaction cost analysis |
| `options/` | [options.md](options.md) | Greeks, Black-Scholes, IV surface |
| `analytics/` | [analytics.md](analytics.md) | ClickHouse analytical queries |
| `data_quality/` | [data_quality.md](data_quality.md) | Data profiling |
| `diagnostics/` | [diagnostics.md](diagnostics.md) | Event replay, decision trace |
| `testing/` | [testing.md](testing.md) | Load generator, fault injection |
| `utils/` | [utils.md](utils.md) | Logging, serialization |
| `scripts/` | [scripts.md](scripts.md) | Operational scripts |
