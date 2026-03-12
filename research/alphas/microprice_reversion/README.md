# microprice_reversion

## Hypothesis
- Microprice-mid deviation predicts short-term mean-reversion: when microprice deviates above mid (bid side heavier), subsequent ticks tend to revert downward, and vice versa.

## Formula
- `signal_t = -EMA_16((microprice_x2 - mid_price_x2) / max(spread_scaled, 1))`

## Metadata
- `alpha_id`: `microprice_reversion`
- `paper_refs`: (none)
- `complexity`: `O(1)`
- `feature_set_version`: `lob_shared_v1`
- `status`: `DRAFT`
