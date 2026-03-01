# lob_shape

## Hypothesis
- LOB depth slope asymmetry (`ask_slope - bid_slope`) captures latent short-horizon pressure.
- When slope asymmetry aligns with OFI/depth-imbalance direction, signal quality improves.

## Formula
- `slope_bid = OLS(level_idx, log(bid_qty + 1))`
- `slope_ask = OLS(level_idx, log(ask_qty + 1))`
- `sign_align = sign(ofi_l1_ema8) == sign(depth_imbalance_ema8_ppm) ? {1,-1,0}`
- `signal = (slope_ask - slope_bid) + lambda * sign_align`

## Metadata
- `alpha_id`: `lob_shape`
- `paper_refs`: `depth_slope_ref` (needs mapping in `research/knowledge/paper_index.json`)
- `complexity`: `O(N)` (`N` = number of levels used in slope fit)
