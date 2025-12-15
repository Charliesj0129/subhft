# Tasks – StrategyRunner

| ID | Title | Description & Acceptance | Dependencies |
| --- | --- | --- | --- |
| S1 | Strategy config & registry | Implement `config/strategies.yaml` schema, loader, and registry that instantiates strategies dynamically. **Acceptance**: CLI lists registered strategies with their params; hot reload works. | — |
| S2 | StrategyContext enhancements | Extend context with LOB references, features, positions, StormGuard state, and helper APIs (`get_lob`, `get_features`, `place_order`) aligned with Shioaji order fields. **Acceptance**: unit test verifies context provides expected data and helper populates Shioaji-compatible intent. | S1 |
| S3 | Runner core refactor | Update `StrategyRunner` to consume bus events, build context per event, execute strategies sequentially, and enforce latency budget. **Acceptance**: integration test with mock strategy ensures events processed and latency logged. | S2 |
| S4 | Intent validation & backpressure | Validate intents (symbol/qty/tif/order type per Shioaji), timestamp them, enqueue to risk queue; implement behavior when queue full (drop/log or throttle). **Acceptance**: test simulates full queue and ensures log + drop policy applied. | S3 |
| S5 | Error handling & disablement | Catch strategy exceptions, log, disable after threshold; provide method/CLI to re-enable. **Acceptance**: test strategy raising error gets disabled and re-enable command works. | S3 |
| S6 | Metrics & logging | Integrate `MetricsRegistry` (latency histograms, intents counters, error counters) and structured logs for lifecycle events. **Acceptance**: `/metrics` shows per-strategy metrics; logs contain registration/disablement entries. | S3 |
| S7 | CLI/control tooling | Add CLI commands (e.g., `python -m hft_platform.strategy.cli list|enable|disable|reload`). **Acceptance**: CLI operations reflected in runner state without restart. | S1, S5 |
| S8 | Documentation & tests | Document strategy development guide (context fields, helper APIs, budgets) and add unit/integration tests covering typical flows. **Acceptance**: docs reviewed; tests pass. | S1–S7 |
