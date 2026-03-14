# regime_momentum

## Hypothesis
- The momentum of the volatility regime factor predicts whether the market is transitioning between regimes; rising regime factor with positive OFI signals trend continuation.

## Formula
- `signal = clip((EMA8(rf) - EMA32(rf)) * sign(ofi_ema8), -2, 2)`
- `rf = clip(vol16 / base64, 0.5, 2.0)`

## Data Fields
- `bid_qty`
- `ask_qty`

## Metadata
- `alpha_id`: `regime_momentum`
- `paper_refs`: 082
- `complexity`: `O(1)`
- `latency_profile`: `shioaji_sim_p95_v2026-03-04`
- `feature_set_version`: `lob_shared_v1`
