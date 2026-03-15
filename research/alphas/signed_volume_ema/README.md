# signed_volume_ema

Volume-weighted directional flow alpha.

## Signal

`SVE_t = EMA_8(volume * (bid_qty - ask_qty) / (bid_qty + ask_qty))`

## Hypothesis

Directional volume (volume signed by queue imbalance direction) reveals net
buying/selling pressure. Sustained signed volume in one direction predicts
price continuation.

## Data Fields

- `volume` -- trade volume
- `bid_qty` -- best bid queue size
- `ask_qty` -- best ask queue size

## Status

DRAFT
