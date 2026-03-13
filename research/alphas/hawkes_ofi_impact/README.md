# hawkes_ofi_impact

## Hypothesis
- Self-exciting order flow clustering amplifies OFI predictive power: when trades arrive in bursts (high Hawkes intensity), the OFI signal carries stronger short-term price prediction.

## Formula
- `signal = EMA_8(OFI) * clip(hawkes_intensity / baseline, 0.5, 2.0)`
- OFI = (bid_qty_change - ask_qty_change)
- Hawkes intensity approximated via exponential kernel EMA (window ~16 ticks)

## Metadata
- `alpha_id`: `hawkes_ofi_impact`
- `paper_refs`: 026
- `complexity`: `O(1)`
