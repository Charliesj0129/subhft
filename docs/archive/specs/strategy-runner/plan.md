# Plan – StrategyRunner

## Components
1. **Strategy Registry & Config Loader**
   - Parse `config/strategies.yaml` (strategy id, module.class, params, budget, enabled flag, instrument list/product type).
   - Dynamically import strategy classes and instantiate with params.

2. **StrategyContext & API Helpers**
   - Extend `StrategyContext` to include references to LOB features, positions, StormGuard state, timer info.
   - Provide helper methods for common actions (fetching book/feature, placing intents).

3. **Runner Core**
   - Event loop consuming from bus (async generator).
   - For each event, build context and iterate strategies.
   - Measure latency using `time.perf_counter_ns`; enforce budget.

4. **Intent Queue Management**
   - Validate intents, timestamp, attach metadata (strategy_id, event seq).
   - Use non-blocking enqueue to risk queue; handle full queue (backpressure, drop policy, logging).

5. **Error Handling & Disablement**
   - Catch exceptions per strategy, log, disable strategy after configurable threshold.
   - Provide CLI/commands to re-enable strategy.

6. **Metrics & Logging**
   - Integrate `MetricsRegistry`: latency histogram, intents counter, error counter, disablement gauge.
   - Structured logs for registration, errors, disable/enable events, queue saturation.

7. **CLI/Control Plane**
   - Commands to list strategies, enable/disable, reload config, view latency stats.

8. **Testing**
   - Unit tests for context building, latency enforcement, intent validation.
   - Integration test with mock strategies generating intents and verifying risk queue outputs.

## Implementation Steps
1. Create strategy config schema and loader.
2. Extend StrategyContext and base Strategy interface.
3. Refactor StrategyRunner to use registry, context injection, latency measurements.
4. Implement intent validation/backpressure handling.
5. Add metrics/logging integration.
6. Build CLI for management.
7. Write tests and documentation.
