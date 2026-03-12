# microprice_momentum

## Signal

```
MM_t = EMA_8( (microprice_x2_t - microprice_x2_{t-1}) / max(spread_scaled, 1) )
```

## Hypothesis

Microprice momentum predicts continued price movement. A rising microprice
signals further upward movement; a falling microprice signals further downward
movement. This is the momentum (trend-following) complement to microprice
reversion.

## Data Fields

- `microprice_x2` (int, scaled) -- volume-weighted microprice from LOBStatsEvent
- `spread_scaled` (int, scaled) -- bid-ask spread from LOBStatsEvent

## State (4 slots)

| Slot            | Type  | Purpose                            |
| --------------- | ----- | ---------------------------------- |
| `_prev_micro`   | float | Previous tick microprice_x2        |
| `_mom_ema`      | float | EMA of normalized momentum         |
| `_signal`       | float | Last emitted signal                |
| `_initialized`  | bool  | Whether first tick has been stored |

## Design Notes

- First update stores `microprice_x2` and returns 0 (no delta available yet).
- Subsequent updates compute `delta = (micro_t - micro_{t-1}) / max(spread, 1)`
  and apply EMA smoothing with alpha = 1 - exp(-1/8).
- Spread normalization makes the signal comparable across instruments with
  different tick sizes.
- All state is scalar (Allocator Law compliance).
