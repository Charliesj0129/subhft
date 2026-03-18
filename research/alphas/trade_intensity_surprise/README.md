# trade_intensity_surprise

## Hypothesis

When trade volume arrives faster than its rolling baseline, conditioned on
queue imbalance direction, it signals informed directional flow.

## Formula

```
vol_fast  += α8  * (volume - vol_fast)
vol_slow  += α64 * (volume - vol_slow)
qi         = (bid_qty - ask_qty) / (bid_qty + ask_qty + ε)
qi_ema    += α8  * (qi - qi_ema)
ir         = log(vol_fast / max(vol_slow, ε))
signal     = qi_ema * ir
```

## Paper References

- 1312.0514 — Lipton et al., "Trade arrival dynamics and quote imbalance in a limit order book"
- 1809.08060 — Morariu-Patrichi & Pakkanen, "State-dependent Hawkes processes and their application to limit order book modelling"

## Status

DRAFT → Gate A → Gate B → Gate C (pending)
