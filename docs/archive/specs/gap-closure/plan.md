# Plan – Gap Closure Roadmap

## Workstreams
1. **Strategy Context & Feature Engine**
   - Inject `LOBEngine` snapshot + `PositionStore` state into `StrategyContext`.
   - Implement feature engine in Rust (pyo3 module) exposing mid/spread/imbalance, queue stats, volatility; wrap via Python proxy.
   - Extend `StrategyContext` with `ctx.features(symbol)` and helper APIs (`ctx.place_order`, `ctx.get_position`).
   - Provide registry/config system to declare strategies and parameters.

2. **Order Adapter & Execution Loop**
   - Implement `live_orders` map, Shioaji SDK calls (place/update/cancel), and event coalescing.
   - Maintain order_id ↔ strategy/intent map for execution reconciliation.
   - Feed broker responses into adapter/risk for rate-limit and circuit-breaker logic.

3. **Execution Normalization & Reconciliation**
   - Add `order_id_map` shared between OrderAdapter and ExecutionNormalizer.
   - Complete `ReconciliationService`: fetch positions via Shioaji APIs, diff with PositionStore, emit corrections.
   - Ensure execution events update StormGuard PnL and recorder.

4. **Recorder & ClickHouse Integration**
   - Replace `AsyncRecorder` stub with batcher/WAL workers; subscribe to bus for market/order/risk/position events.
   - Implement per-table routing and micro-batching, WAL spillover, and ClickHouse inserts per schema.
   - Add retention/TTL maintenance scripts.

5. **Backtest & Analytics Runtime**
   - Build `ClickHouseReplayFeed`, `SimulatedBroker`, `BacktestRunner`, CLI, and notebooks.
   - Write results to `backtest_runs` & `backtest_timeseries` tables; add analytics views.

6. **Observability**
   - Expose `/metrics` endpoint, integrate metrics/logging across components, add recorder/ClickHouse health metrics.
   - Commit Grafana dashboards + Prometheus alert rules; document runbooks.

7. **Docs & Onboarding**
   - Publish `docs/strategy-guide.md`, quickstart README updates, FAQ.
   - Provide example strategies and CLI scaffolding.

## Milestones
1. **M1 – Online Path Closure**
   - StrategyContext wiring, OrderAdapter real broker calls, execution map, reconciliation.
2. **M2 – Persistence & Observability**
   - Recorder/WAL/ClickHouse integration, metrics endpoint, alerts/dashboards.
3. **M3 – Backtest & Feature Layer**
   - Backtest runtime, Rust feature engine, strategy guide + samples.
