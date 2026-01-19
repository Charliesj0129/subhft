# risk

## Purpose
Pre-trade risk checks and safety controls.

## Key Files
- `src/hft_platform/risk/engine.py`: `RiskEngine` pipeline.
- `src/hft_platform/risk/validators.py`: `PriceBandValidator`, `MaxNotionalValidator`.
- `src/hft_platform/risk/storm_guard.py`: `StormGuardFSM` state machine.
- `src/hft_platform/risk/base.py`: Base interfaces.

## Flow
1) Receive `OrderIntent` from strategy.
2) Validate against limits.
3) If approved, emit `OrderCommand`.
4) If rejected, emit metrics and logs.

## Configuration
- `config/strategy_limits.yaml`
- `config/risk.yaml`

## Extension Points
- Add new validators.
- Add strategy-level overrides.
