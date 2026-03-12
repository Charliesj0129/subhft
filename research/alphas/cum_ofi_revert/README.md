# cum_ofi_revert

## Hypothesis
Cumulative OFI is mean-reverting on longer horizons. Large positive cumOFI
signals upward overextension (fade with short); large negative cumOFI signals
downward overextension (fade with long).

## Formula
`-EMA_16( ofi_l1_cum / max(EMA_64(|ofi_l1_cum|), 1) )`

## Metadata
- `alpha_id`: `cum_ofi_revert`
- `data_fields`: `ofi_l1_cum`
- `complexity`: `O(1)`
- `latency_profile`: `shioaji_sim_p95_v2026-03-04`
