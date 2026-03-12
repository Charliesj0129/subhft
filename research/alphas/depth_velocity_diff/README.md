# depth_velocity_diff

## Hypothesis
- Order-flow signals from deeper LOB levels add predictive value versus top-of-book-only signals for the next tick return.

## Formula
- `alpha_t = sum_k (w_k * ofi_k_t), where w_k = 1 / max(1, k)`

## Data Fields
- `l1_bid_qty`
- `l1_ask_qty`
- `depth_imbalance_ppm`
- `depth_imbalance_ema8_ppm`
- `spread_scaled`
- `mid_price_x2`
- `microprice_x2`
- `l1_imbalance_ppm`
- `spread_ema8_scaled`

## Metadata
- `alpha_id`: `depth_velocity_diff`
- `paper_refs`: 039
- `complexity`: `O(1)`
