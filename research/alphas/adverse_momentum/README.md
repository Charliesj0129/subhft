# adverse_momentum — Adverse Selection Momentum (OFI-Return Residual)

## Paper References
- Paper 131: Cartea, Sanchez-Betancourt (2025) "A Simple Strategy to Deal with Toxic Flow" (arxiv:2503.18005)
- Paper 136: Barzykin, Boyce, Neuman (2024) "Unwinding Toxic Flow with Partial Information" (arxiv:2407.04510)

## Hypothesis
When realized micro-returns consistently exceed the return predicted by OFI alone,
informed traders are present; the signed residual (direction from OFI, magnitude
from unexplained return) captures the "hidden alpha process" from Cartea (2025).

## Formula
```
delta_mid = mid_price - prev_mid
beta = EMA_32(ofi * delta_mid) / max(EMA_32(ofi^2), eps)
expected = beta * ofi_l1_ema8
residual = delta_mid - expected
signal = EMA_8(sign(ofi) * |residual|), clipped to [-2, 2]
```

## Data Fields
- `mid_price` (scaled int x10000)
- `ofi_l1_ema8` (float, from FeatureEngine)
- `spread_scaled` (int, reserved for future spread-gating)

## Status
DRAFT
