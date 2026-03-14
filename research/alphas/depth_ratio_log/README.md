# depth_ratio_log

## Hypothesis
- Log-ratio of bid/ask depth is a symmetric measure of directional pressure;
  log transform makes it additive and centered at zero.

## Formula
- `signal = clip(EMA_8(log(max(bid_qty, 1) / max(ask_qty, 1))), -2, 2)`

## Data Fields
- `bid_qty`
- `ask_qty`

## Metadata
- `alpha_id`: `depth_ratio_log`
- `paper_refs`: 032 (arXiv 2601.19369)
- `complexity`: `O(1)`
- `latency_profile`: `shioaji_sim_p95_v2026-03-04`
