# cross_ema_qi

## Hypothesis
- EMA crossover (fast vs slow) of queue imbalance detects momentum shifts earlier than single-EMA smoothing.

## Formula
- `qi = (bid - ask) / max(bid + ask, 1); fast = EMA_4(qi); slow = EMA_16(qi); signal = clip(fast - slow, -1, 1)`

## Data Fields
- `bid_qty`
- `ask_qty`

## Metadata
- `alpha_id`: `cross_ema_qi`
- `paper_refs`: 127
- `complexity`: `O(1)`
