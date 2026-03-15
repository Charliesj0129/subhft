# ofi_volume_ratio

## Hypothesis
- Order flow imbalance normalized by volume separates informed from noise flow.
  High |OFI/volume| indicates directional conviction; low ratio indicates random noise.

## Formula
- `OVR_t = EMA_8((bid_qty - ask_qty) / max(volume, epsilon))`

## Metadata
- `alpha_id`: `ofi_volume_ratio`
- `data_fields`: `bid_qty`, `ask_qty`, `volume`
- `complexity`: `O(1)`
