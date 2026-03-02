# ofi_execution_126

## Hypothesis
- Signed order-flow imbalance predicts short-horizon price pressure, especially when queue imbalance aligns with the OFI direction.

## Formula
- `alpha_t = zscore(ofi_l1_ema8_t) * sign(depth_imbalance_ema8_ppm_t)`

## Data Fields
- `ofi_l1_raw`
- `ofi_l1_cum`
- `ofi_l1_ema8`
- `depth_imbalance_ppm`
- `depth_imbalance_ema8_ppm`
- `l1_bid_qty`
- `l1_ask_qty`
- `spread_scaled`
- `spread_ema8_scaled`
- `mid_price_x2`
- `microprice_x2`
- `l1_imbalance_ppm`

## Metadata
- `alpha_id`: `ofi_execution_126`
- `paper_refs`: 126
- `complexity`: `O(1)`
