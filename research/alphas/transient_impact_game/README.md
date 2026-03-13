# transient_impact_game

## Hypothesis
Transient price impact from order flow decays predictably. High transient-to-total impact ratio signals temporary flow that will revert, providing a contrarian trading signal.

## Formula
```
ofi = delta(bid_qty) - delta(ask_qty)
transient_impact = transient_impact * (1 - decay_rate) + abs(ofi)
total_impact_ema += alpha * (abs(ofi) - total_impact_ema)
ratio = clip(-transient_impact / (total_impact_ema + eps), -1, 0)
signal = EMA_8(ratio)
```

## Metadata
- `alpha_id`: `transient_impact_game`
- `paper_refs`: 013
- `complexity`: `O(1)`
