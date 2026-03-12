# toxic_flow

## Hypothesis
Simultaneous OFI surge and spread widening indicates toxic informed flow;
following the informed direction is predictive of short-term price movement.

## Formula
```
signal_t = sign(ofi_ema8) x EMA_8(|QI| x spread_scaled / max(EMA_64(spread_scaled), 1))
```
where QI = (bid_qty - ask_qty) / (bid_qty + ask_qty + eps).

## Metadata
- `alpha_id`: `toxic_flow`
- `paper_refs`: Inspired by Easley, Lopez de Prado & O'Hara (2012) VPIN
- `complexity`: `O(1)`
- `tier`: TIER_2
- `data_fields`: bid_qty, ask_qty, spread_scaled, ofi_l1_ema8
- `latency_profile`: shioaji_sim_p95_v2026-03-04
- `feature_set_version`: lob_shared_v1
