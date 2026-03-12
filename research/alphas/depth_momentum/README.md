# depth_momentum

## Hypothesis
- Total depth change rate predicts liquidity regime shifts; rising total depth signals improving liquidity (mean-reverting), falling depth signals deteriorating liquidity (trending).

## Formula
- `signal = clip(EMA16(delta) / max(EMA64(|delta|), eps), -2, 2)` where `delta = (bid_qty + ask_qty) - prev_total`

## Data Fields
- `bid_qty`
- `ask_qty`

## Metadata
- `alpha_id`: `depth_momentum`
- `paper_refs`: 013
- `complexity`: `O(1)`
