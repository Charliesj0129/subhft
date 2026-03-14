# toxicity_multiscale

Multi-timescale Toxicity Composite alpha signal.

**Paper**: 129 — Cartea, Duran-Martin, Sanchez-Betancourt (2023) "Detecting Toxic Flow" (arxiv:2312.05827)

## Formula

```
QI = (bid_qty - ask_qty) / (bid_qty + ask_qty + eps)
volatility = EMA_16(|delta_mid|)
spread_dev = spread_scaled / max(EMA_64(spread_scaled), 1)
raw = volatility * |QI| * spread_dev
signal = sign(QI) * EMA_8(raw), clipped to [-2, 2]
```

## Status

DRAFT — not yet promoted through Gate A.
