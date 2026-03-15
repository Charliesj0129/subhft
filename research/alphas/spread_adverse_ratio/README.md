# spread_adverse_ratio

## Hypothesis
The fraction of bid-ask spread attributable to adverse selection (after removing the volatility-driven component) measures the toxicity regime; high adverse selection ratio indicates informed traders are active and market makers widen spreads beyond what volatility alone justifies.

## Formula
```
delta_mid = |mid_price - prev_mid|
vol_proxy = delta_mid * sqrt(volume)
vol_proxy_ema = EMA_16(vol_proxy)
vol_component = kappa * vol_proxy_ema
adverse_component = spread_scaled - vol_component
signal = clip(adverse_component / max(spread_scaled, 1), 0, 1)
```

## Metadata
- `alpha_id`: `spread_adverse_ratio`
- `paper_refs`: 131 (Cartea & Sanchez-Betancourt 2025), Glosten & Milgrom 1985
- `complexity`: `O(1)`
- `tier`: TIER_2
- `signal_range`: [0, 1] (unsigned regime indicator)
