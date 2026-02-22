# garch_vol

## Hypothesis
- Online conditional volatility is a stable state feature for gating and position sizing.
- Lightweight GARCH(1,1) update provides low-latency volatility forecast.

## Formula
- `sigma^2_t = omega + alpha * r^2_{t-1} + beta * sigma^2_{t-1}`
- `signal_t = sqrt(sigma^2_t)`

## Metadata
- `alpha_id`: `garch_vol`
- `paper_refs`: 021
- `complexity`: `O(1)`
