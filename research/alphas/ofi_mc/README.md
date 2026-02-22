# ofi_mc

## Hypothesis
- Best bid/ask queue changes encode short-horizon directional pressure.
- Normalizing cumulative OFI by market-cap proxy improves robustness vs pure volume normalization.

## Formula
- `OFI_t = BidFlow_t - AskFlow_t`
- `signal_t = cumulative(OFI_t) / market_cap`

## Metadata
- `alpha_id`: `ofi_mc`
- `paper_refs`: 018
- `complexity`: `O(1)`
