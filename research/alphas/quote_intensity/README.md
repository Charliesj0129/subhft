# quote_intensity

## Hypothesis
- The rate of quote (bid/ask) changes signals information arrival intensity.
- Rapid quote updates indicate active repositioning by informed market makers.
- Quote intensity multiplied by imbalance direction predicts next-tick moves.

## Formula
- `QI_t = EMA_8(|delta_bid_qty| + |delta_ask_qty|) * sign(bid_qty - ask_qty) / EMA_32(|delta_bid_qty| + |delta_ask_qty|)`

## Metadata
- `alpha_id`: `quote_intensity`
- `paper_refs`: (none)
- `complexity`: `O(1)`
