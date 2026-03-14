# mean_revert_qi

## Hypothesis
- Queue imbalance tends to mean-revert; extreme deviations from the long-term mean predict contrarian price moves.

## Formula
- `signal = -clip((qi - EMA64(qi)) / sqrt(EMA32((qi - EMA64(qi))^2)), -2, 2)`
- `qi = (bid_qty - ask_qty) / max(bid_qty + ask_qty, 1)`

## Data Fields
- `bid_qty`
- `ask_qty`

## Metadata
- `alpha_id`: `mean_revert_qi`
- `paper_refs`: 098
- `complexity`: `O(1)`
- `latency_profile`: `shioaji_sim_p95_v2026-03-04`
- `feature_set_version`: `lob_shared_v1`
