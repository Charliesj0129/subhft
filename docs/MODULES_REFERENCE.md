# Modules Reference (Consolidated)

> **For code-level gotchas and patterns**, see `.agent/memory/module_gotchas.md`.

This document is a compressed directory map of `src/hft_platform/`. It describes the responsibilities of each Python subpackage, serving as a unified reference point and drastically reducing token bloat.

## Hot Path Modules (Latency-Critical)

These modules run the primary event loop. Any changes here must adhere strictly to the **Precision Law** (no floats for prices) and **Allocator Law** (no heap allocations per tick).

| Subpackage       | Key Classes / Files                                       | Responsibility                                                                                                                                                       |
| ---------------- | --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **feed_adapter** | `ShioajiClient`, `MarketDataNormalizer`, `LOBEngine`      | Ingests broker data. Normalizes to `TickEvent`/`BidAskEvent`. Maintains Level 2 Book State. Emits `LOBStatsEvent`. Contains extensive Rust fast-paths (`rust_core`). |
| **engine**       | `RingBufferBus`                                           | Event bus using Disruptor-pattern ring buffer (`publish_nowait`, `consume`). Routes MD events to StrategyRunner.                                                     |
| **strategy**     | `BaseStrategy`, `StrategyRunner`, `StrategyContext`       | Houses the `StrategyRunner` which consumes bus events, triggers strategy user logic (`handle_event`), and outputs `OrderIntent`.                                     |
| **risk**         | `RiskEngine`, `StormGuardFSM`, validators                 | Synchronous CPU checks. Evaluates `OrderIntent`. If approved, outputs `OrderCommand`. `StormGuardFSM` handles global HALT/WARM risk states.                          |
| **order**        | `OrderAdapter`, `CircuitBreaker`, `SlidingWindowLimiter`  | Outbound flow. Validates rate limits, circuit breakers, and sends `OrderCommand` to `ShioajiClient.place_order()`.                                                   |
| **execution**    | `ExecutionRouter`, `PositionStore`, `ExecutionNormalizer` | Normalizes incoming fills/callbacks. Tracks position in O(1) time using ONLY integers (`RustPositionTracker`). Publishes `PositionDelta`.                            |
| **gateway**      | `GatewayService`, `ExposureStore`, `GatewayPolicy`        | (CE-M2 only) Optional HA gateway to serialize intents. Handles Dedup, Policy, and Exposure Limits. Default disabled (`HFT_GATEWAY_ENABLED=0`).                       |

## Data & Event Models (Contracts)

| Subpackage    | Key Classes / Files                                         | Responsibility                                                                                 |
| ------------- | ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| **events.py** | `TickEvent`, `BidAskEvent`, `LOBStatsEvent`                 | Data structures flowing through the `EventBus`. Prices are **scaled `int` (x10000)**.          |
| **contracts** | `OrderIntent`, `OrderCommand`, `FillEvent`, `PositionDelta` | Inter-module boundaries for strategies and execution logic. Never pass float/Decimal directly. |
| **features**  | `ofi.py`, `micro_price.py`, `fractal.py`                    | Market microstructure feature computations stored in `StrategyContext`.                        |

## Infrastructure & Control Plane

| Subpackage        | Key Classes / Files                                    | Responsibility                                                                                              |
| ----------------- | ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| **services**      | `HFTSystem`, `SystemBootstrapper`, `MarketDataService` | App lifecycle and orchestration. `MarketDataService` runs the watchdog and reconnect FSM (15s/60s/300s).    |
| **config**        | `loader.py`, `wizard.py`, `symbols.yaml`               | Layered settings merge (`base` -> `env` -> runtime `HFT_*`). Hot-reloads symbol metadata.                   |
| **recorder**      | `RecorderService`, `WALFirstWriter`, `schema.py`       | Durable storage pipeline. Writes `market_data`, `orders`, `fills` to ClickHouse or localized `.wal` format. |
| **migrations**    | `clickhouse/*.sql`                                     | SQL schema management for the ClickHouse backend. Auto-applied on boot.                                     |
| **core**          | `timebase`, `pricing`, `market_calendar`               | `now_ns()` (ALWAYS use this, not `time.time()`). `PriceCodec` (Scales floats to x10000 ints).               |
| **observability** | `MetricsRegistry`, `LatencyRecorder`                   | Prometheus metrics. See `HFT_OBS_POLICY` for hotpath sampling rates (minimal/balanced/debug).               |
| **ipc**           | `ShmRingBuffer`                                        | Shared memory SPSC ring buffer for inter-process comms. Uses Numba JIT.                                     |

## Application Layer

| Subpackage         | Key Classes / Files                     | Responsibility                                                                             |
| ------------------ | --------------------------------------- | ------------------------------------------------------------------------------------------ |
| **cli** / **main** | `cli.py`, `__main__.py`                 | CLI Entry point. Commands: `run sim`, `run live`, `init`, `strat test`, `check`.           |
| **strategies**     | `simple_mm.py`, `rust_alpha.py`         | Built-in implementations of strategies. You can use these as templates.                    |
| **backtest**       | `adapter.py`, `convert.py`, `runner.py` | Integration with `hftbacktest`. Converts JSONL → NPZ and generates HTML scorecard reports. |
