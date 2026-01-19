# features

## Purpose
Market microstructure feature computation for strategies.

## Key Files
- `src/hft_platform/features/micro_price.py`
- `src/hft_platform/features/ofi.py`
- `src/hft_platform/features/entropy.py`
- `src/hft_platform/features/fractal.py`
- `src/hft_platform/features/liquidity.py`
- `src/hft_platform/features/advanced_liquidity.py`

## Inputs and Outputs
- Inputs: LOB levels, tick events, or derived stats.
- Outputs: numeric features stored in `StrategyContext`.

## Extension Points
- Add a new feature module and wire it in `strategy/factors.py` (if used).
