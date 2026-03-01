# alpha_mid_price_v1

## Hypothesis
- Short-horizon microstructure imbalance can predict near-term mid-price direction after controlling for spread and depth.

## Formula
- `alpha_t = (depth_imbalance_ppm_t / 1e6) - 0.5 * (spread_scaled_t / max(mid_price_x2_t, 1))`

## Data Fields
- `spread_scaled`
- `depth_imbalance_ppm`
- `l1_bid_qty`
- `l1_ask_qty`
- `mid_price_x2`

## Metadata
- `alpha_id`: `alpha_mid_price_v1`
- `paper_refs`: N/A
- `complexity`: `O(1)`
