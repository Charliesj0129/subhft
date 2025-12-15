# Tasks – Gap Closure

| ID | Title | Description & Acceptance | Dependencies |
| --- | --- | --- | --- |
| G1 | Wire StrategyContext with LOB & Positions | Inject live `LOBEngine` snapshots and `PositionStore` state into `StrategyContext`; expose feature proxy APIs. **Acceptance**: sample strategy reads mid/spread/position data; unit test ensures context populated. | — |
| G2 | Implement Rust feature engine interface | Build `rust_core` pyo3 module exposing feature computations, wrap via Python proxy, and publish `MarketFeature` events. **Acceptance**: Rust module returns features for top-5 LOB; Python strategies access via `ctx.features(symbol)`. | G1 |
| G3 | Complete OrderAdapter integration | Add `live_orders` map, Shioaji place/update/cancel calls, coalescing, and feedback metrics. **Acceptance**: adapter submits real orders in simulation tests, rate limits enforced, circuit breaker toggles on repeated failures. | G1 |
| G4 | Execution mapping & reconciliation | Maintain order→strategy map, enrich `ExecutionNormalizer`, and implement `ReconciliationService` diff logic. **Acceptance**: broker callbacks mapped to correct strategy IDs; reconciliation updates PositionStore when mismatches detected. | G3 |
| G5 | Recorder batching & ClickHouse wiring | Replace recorder stub with batcher/WAL, subscribe to bus, route events to ClickHouse tables, and add retention scripts. **Acceptance**: end-to-end test writes market/order/risk events, WAL triggers on simulated outage, data visible in CH. | G4 |
| G6 | Backtest runtime implementation | Create `ClickHouseReplayFeed`, `SimulatedBroker`, `BacktestRunner`, CLI, and notebooks; add `backtest_runs/timeseries` schemas. **Acceptance**: CLI run produces run ID, summary/time-series rows inserted, notebooks visualize results. | G5 |
| G7 | Observability completion | Expose `/metrics`, add recorder/ClickHouse metrics, commit Grafana dashboards and alert rules. **Acceptance**: Prometheus scrapes metrics, alerts fire in failure drills, dashboards display recorder/ClickHouse health. | G5 |
| G8 | Strategy guide & tooling | Write `docs/strategy-guide.md`, update README quickstart, add example strategies and CLI scaffolding. **Acceptance**: new user can follow guide to create strategy, run backtest, deploy to live. | G6 |
