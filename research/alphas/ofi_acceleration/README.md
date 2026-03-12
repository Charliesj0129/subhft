# ofi_acceleration

## Signal

`EMA_8(ofi_l1_raw_t - ofi_l1_raw_{t-1})`

## Hypothesis

Second derivative of order flow (acceleration) predicts momentum shifts.
Positive acceleration = increasing buy pressure = upward price movement.
Deceleration = potential reversal.

## Data Fields

- `ofi_l1_raw` -- Level-1 order flow imbalance (raw, from FeatureEngine)

## State

4 scalar slots: `_prev_ofi`, `_accel_ema`, `_signal`, `_initialized`

## Complexity

O(1) per tick. No heap allocations.

## Status

DRAFT -- pending Gate A/B validation.
