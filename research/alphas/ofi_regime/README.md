# ofi_regime — Regime-Dependent OFI Elasticity

## Hypothesis

OFI (Order Flow Imbalance) predictive power (impact coefficient) is
regime-dependent: it amplifies during high-volatility periods and attenuates
during low-volatility periods. A regime-scaled EMA-OFI signal outperforms a
fixed-weight EMA-OFI baseline.

Evidence from Paper 123 (CSI 300 index futures) shows OFI follows an OU
process with Lévy jumps, and the price impact coefficient β varies across
volatility regimes. Asian index futures (TXFB/TAIFEX) share similar
microstructure dynamics.

## Formula

```
ofi       = (bid_qty - ask_qty) / max(bid_qty + ask_qty, 1)
ofi_ema8  += α8  * (ofi - ofi_ema8)          # α8  = 1 - exp(-1/8)  ≈ 0.1175
vol16     += α16 * (|ofi| - vol16)            # α16 = 1 - exp(-1/16) ≈ 0.0606
base64    += α64 * (vol16 - base64)           # α64 = 1 - exp(-1/64) ≈ 0.0154
rf        = clip(vol16 / max(base64, 1e-8), 0.5, 2.0)
signal    = ofi_ema8 * rf                     ∈ [-2, 2]
```

### Regime Factor Interpretation

| rf value | Interpretation |
|----------|----------------|
| ≈ 1.0 | Neutral — current vol matches baseline |
| → 2.0 | High-vol regime — OFI directional impact amplified |
| → 0.5 | Low-vol regime — OFI signal attenuated |

## Paper References

- **Paper 123**: arXiv 2505.17388v1 — *Stochastic Price Dynamics in Response to
  Order Flow Imbalance: Evidence from CSI 300 Index Futures* (OU/Lévy OFI model,
  regime-dependent β)
- **Paper 122**: OFI price impact coefficient estimation

## Implementation

- **Complexity**: O(1) per tick (3 float EMA states, `__slots__`)
- **Data fields**: `bid_qty`, `ask_qty`
- **Latency profile**: `shioaji_sim_p95_v2026-03-04`
- **Feature set version**: `lob_shared_v1`

## Status

DRAFT → Target Gate C with synthetic UL5 v2 data.

## Synthetic Data

Generated with `SyntheticLOBConfigV2` (OU-Hawkes-Markov model, Papers 026/039/120/121/122/123):

```bash
python -m research.tools.synth_lob_gen \
  --version v2 --n-rows 20000 --rng-seed 42 \
  --out research/data/processed/ofi_regime/ofi_regime_synth_v1.npy
```

## Gate C Validation

```bash
python -m research.factory run-gate-c ofi_regime \
  --data research/data/processed/ofi_regime/ofi_regime_synth_v1.npy \
  --latency-profile shioaji_sim_p95_v2026-03-04
```
