# pregeometric_lob

## Hypothesis
- Gamma distribution shape parameter of LOB depth reveals liquidity concentration.
- Concentrated bid-side support (high shape) vs dispersed ask-side signals upward price pressure.

## Formula
- `gamma_shape(side) = mean(qty)^2 / var(qty)` (method-of-moments estimator)
- `signal = EMA_8(gamma_shape_bid - gamma_shape_ask)` bounded to [-2, 2]

## Metadata
- `alpha_id`: `pregeometric_lob`
- `paper_refs`: 092
- `complexity`: `O(N)` per tick (N = depth levels)
