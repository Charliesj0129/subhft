# liquidity_shear

**Paper ref**: 032
**Status**: DRAFT
**Tier**: ENSEMBLE

## Signal

Measures asymmetry in LOB depth profile shape between bid and ask sides.
The "geometric shear" is the log-ratio of bid vs ask depth decay rates.

```
depth_slope_k = total_qty / weighted_position_sum
shear = log(ask_depth_slope / bid_depth_slope)
signal = EMA_8(shear)
```

Positive shear signals buying pressure (ask-side depth concentrated near BBO).
Negative shear signals selling pressure (bid-side depth concentrated near BBO).

## Data fields

- `bids`: np.ndarray shape (N, 2) — [price, qty] per level
- `asks`: np.ndarray shape (N, 2) — [price, qty] per level

## Complexity

O(N) per tick where N = number of depth levels (typically 5-10).
