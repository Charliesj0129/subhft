# spread_recovery

## Hypothesis
Speed of spread recovery after widening indicates market resilience.
Fast recovery (positive signal after widening) = healthy liquidity.

## Formula
```
spread_dev = spread_scaled - EMA_32(spread_scaled)
peak_dev = max(peak_dev * 0.99, abs(spread_dev))
delta_spread = spread_scaled - prev_spread
recovery_raw = -delta_spread / max(peak_dev, 1)
signal = EMA_16(recovery_raw)
```

## Metadata
- `alpha_id`: `spread_recovery`
- `data_fields`: `("spread_scaled",)`
- `complexity`: `O(1)`
- `tier`: TIER_2
- `feature_set_version`: `lob_shared_v1`
