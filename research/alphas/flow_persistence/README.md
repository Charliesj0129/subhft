# flow_persistence

## Hypothesis
- Agreement between recent and longer-term OFI direction (persistence) predicts trend continuation; disagreement predicts reversals.

## Formula
- `qi = (bid_qty - ask_qty) / max(bid_qty + ask_qty, 1)`
- `fast = EMA_4(qi); slow = EMA_32(qi)`
- `agreement = fast * slow`
- `signal = clip(EMA_8(agreement), -1, 1)`

## Data Fields
- `bid_qty`
- `ask_qty`

## Metadata
- `alpha_id`: `flow_persistence`
- `paper_refs`: 089
- `complexity`: `O(1)`
