# hf_exec_sop_demo

## Hypothesis
- Depth imbalance combined with queue pressure predicts short-horizon direction, while wide spread should dampen conviction.

## Formula
- `alpha_t = (0.7 * depth_imbalance_ppm_t / 1e6 + 0.3 * queue_imbalance_t) / (1 + spread_scaled_t / 1e6)`

## Data Fields
- `depth_imbalance_ppm`
- `l1_bid_qty`
- `l1_ask_qty`
- `spread_scaled`
- `mid_price_x2`

## Metadata
- `alpha_id`: `hf_exec_sop_demo`
- `paper_refs`: 128
- `complexity`: `O(1)`
