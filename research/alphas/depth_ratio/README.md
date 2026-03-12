# depth_ratio Alpha

## Signal

```
DR_t = EMA_8( log( max(bid_depth, 1) / max(ask_depth, 1) ) )
```

## Hypothesis

Log depth ratio is a smoother representation of book asymmetry than linear
imbalance. Log scale handles extreme depth distributions and is more stable.

## Data Fields

- `bid_depth`: total depth on bid side
- `ask_depth`: total depth on ask side

## State (3 slots)

- `_log_ratio_ema`: EMA of the log depth ratio
- `_signal`: cached signal value
- `_initialized`: whether the first update has occurred

## Properties

- **Monotonic**: signal sign matches direction of depth dominance
- **Log-compressed**: extreme ratios (e.g., 1000:1) produce finite, bounded-growth signals
- **Smoother than linear**: `log(2) ~ 0.69 < 1.0` for a 2:1 ratio, reducing noise sensitivity
