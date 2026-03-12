# depth_momentum

## Hypothesis
- Rate of change in LOB depth imbalance predicts near-term price direction: accelerating bid-side depth growth signals upward pressure before price moves.

## Formula
- `signal_t = EMA_8((depth_imb_t - depth_imb_{t-1}))` where `depth_imb = (bid_depth - ask_depth) / (bid_depth + ask_depth + eps)`

## Metadata
- `alpha_id`: `depth_momentum`
- `paper_refs`: (original research)
- `complexity`: `O(1)`
- `data_fields`: `bid_depth`, `ask_depth`
