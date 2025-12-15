# Checklist – StrategyRunner

- [ ] Strategy registry loads from config and supports dynamic enable/disable.
- [ ] StrategyContext exposes LOB features, positions, and helper APIs.
- [ ] Runner enforces per-strategy latency budgets and logs warnings on violations.
- [ ] Intents validated, timestamped, and enqueued; backpressure handled gracefully.
- [ ] Exceptions disable misbehaving strategies and can be re-enabled via CLI.
- [ ] Metrics/logging cover latency, intents, errors, disablements.
- [ ] CLI tools allow listing/enabling/disabling/reloading strategies.
- [ ] Documentation and tests cover strategy development workflow.
