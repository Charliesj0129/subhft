# spread_mean_revert

Spread mean-reversion alpha signal.

## Signal

`-EMA_8((spread_scaled - EMA_64(spread_scaled)) / max(EMA_64(spread_scaled), 1))`

## Hypothesis

Spread deviations from their long-term EMA baseline are mean-reverting. A wide spread (above baseline) signals upcoming contraction; a narrow spread (below baseline) signals upcoming widening. The negative sign fades the deviation.

## Data Fields

- `spread_scaled` (from `lob_shared_v1` feature set)

## State

4 scalar slots: `_spread_ema64`, `_dev_ema8`, `_signal`, `_initialized`

## Status

DRAFT -- pending Gate A/B validation.
