# strategy

## Purpose
Strategy SDK, routing, and context utilities.

## Key Files
- `src/hft_platform/strategy/base.py`: `BaseStrategy`, `StrategyContext`.
- `src/hft_platform/strategy/runner.py`: Event routing and intent creation.
- `src/hft_platform/strategy/registry.py`: Strategy lookup and registry.
- `src/hft_platform/strategy/factors.py`: Feature wiring helpers.
- `src/hft_platform/strategy/cli.py`: Optional strategy CLI helpers.

## Inputs and Outputs
- Inputs: `TickEvent`, `BidAskEvent`, `LOBStatsEvent`.
- Outputs: `OrderIntent` list.

## Extension Points
- Implement new strategies and register with the registry.
- Add new factors in `factors.py`.
