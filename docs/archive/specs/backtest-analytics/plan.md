# Plan – Backtest & Analytics

## Architecture Overview
1. **Backtest Runtime Package (`hft_platform.backtest`)**
   - `ClickHouseReplayFeed`: streams normalized `MarketEvent`s from ClickHouse based on symbol/date filters. Supports chunked queries (e.g., per symbol-day) and yields events in chronological order.
   - `SimulatedBroker`: replaces `OrderAdapter` in backtest mode, using hftbacktest or in-house execution logic to process intents, apply latency/slippage, and emit order/fill events back to the bus.
   - `BacktestRunner`: orchestrates feed → StrategyRunner → Risk → SimulatedBroker → PositionStore, collects outputs, and writes summary/time-series results.
   - `BacktestConfig`: YAML/JSON schema capturing data filters, strategy params, latency/slippage profiles, parallelization options.

2. **Data Access Layer**
   - ClickHouse client wrapper with streaming queries (e.g., `SELECT ... ORDER BY ts LIMIT ...`).
   - Chunked iteration by `(symbol, date)` or `(date, time bucket)` to keep memory bounded.
   - Optional export utility to dump query results to Parquet/Arrow for repeated runs.

3. **Latency & Execution Modeling**
   - Configuration for constant latency + jitter; apply to feed timestamps and order dispatch.
   - Simulated broker maintains LOB snapshots bound to top-5 levels; executes orders by consuming queue depth and modeling passive fills.
   - Configurable slippage/queue position models (starting simple, extendable).

4. **Analytics Outputs**
   - Writers for `backtest_runs` and `backtest_timeseries` tables in ClickHouse.
   - Additional derived views (order behavior, slippage, PnL attribution) defined via SQL/Materialized Views.
   - Notebook templates referencing these outputs for analysis.

5. **Tooling & Interfaces**
   - Python API (`BacktestRunner.run(config)`) returning run ID and summary.
   - CLI (`python -m hft_platform.backtest.cli --config configs/backtest.yaml`).
   - Jupyter notebooks for feature engineering, parameter sweeps, result visualization.
   - Parallel execution harness (joblib/dask) to run multiple configs/symbols concurrently.

6. **Observability & Reproducibility**
   - Log run metadata (config hash, git commit, random seed) with run outputs.
   - Emit metrics for progress (events processed, events/sec, chunk completion) to Prometheus or logs.
   - Support checkpoints/resume for long runs by persisting state boundaries (optional v2).

## Implementation Steps
1. **Schema Work**
   - Add ClickHouse DDLs for `backtest_runs`, `backtest_timeseries`, and supporting views (order behavior, PnL attribution).
2. **Data Access Layer**
   - Implement ClickHouse streaming client with chunked iteration and optional Parquet export.
3. **Replay Feed**
   - Build `ClickHouseReplayFeed` that reads `market_data` (and optional other tables) and publishes events onto the existing bus interface.
4. **Simulated Broker**
   - Implement order execution logic with latency/slippage modeling, producing `OrderEvent`/`FillEvent` identical to live.
   - Integrate optional hftbacktest components if available.
5. **BacktestRunner & CLI**
   - Wire StrategyRunner + Risk + SimulatedBroker + PositionStore into a `BacktestRunner`.
   - Provide CLI & config parsing for running backtests with various parameters.
6. **Analytics Output Writers**
   - Implement summary/time-series collectors and insert into ClickHouse tables.
   - Provide helper functions to query these tables for reports/notebooks.
7. **Notebook Templates**
   - Create Jupyter notebooks demonstrating factor analysis, slippage studies, and run result visualization.
8. **Parallel Execution Support**
   - Add utility to distribute runs across multiprocess/dask (e.g., grid search over parameters).
9. **Testing & Validation**
   - Unit tests for replay feed ordering, simulated broker execution, latency modeling.
   - Integration tests running a short backtest vs sample data, validating outputs.
   - Performance tests to ensure streaming handles target event rates.

## Testing Strategy
- **Unit Tests**: data chunking/order, simulated broker fill logic, latency calculation, run output persistence.
- **Integration Tests**: run small backtest (e.g., 15 min sample) and verify:
  - Strategies run deterministically.
  - Order/fill events generated match expected scenario.
  - `backtest_runs`/`timeseries` entries created.
- **Performance Tests**: stress streaming & simulation with millions of events.
- **Reproducibility**: verify same config/run ID yields identical outputs; capture random seeds.

## Open Questions
- How to calibrate latency/slippage models (live stats vs synthetic)? Need collaboration with trading team.
- Do we need real-time backtest progress UI (for long runs)? Optional future work.
- Should aggregated analytics tables (e.g., bar features) live in ClickHouse or separate data lake?
