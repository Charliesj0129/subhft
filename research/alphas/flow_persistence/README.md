# flow_persistence

## Hypothesis
- Short-horizon microstructure imbalance can predict near-term mid-price direction after controlling for spread and depth.

## Formula
- `alpha_t = zscore(depth_imbalance_ppm_t) - 0.5 * zscore(spread_scaled_t)`

## Data Fields
- `spread_scaled`
- `depth_imbalance_ppm`
- `l1_bid_qty`
- `l1_ask_qty`
- `mid_price_x2`

## Metadata
- `alpha_id`: `flow_persistence`
- `paper_refs`: 089
- `complexity`: `O(1)`
