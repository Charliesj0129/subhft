# C74 — TXF-TMF Basis Mean-Reversion (Cross-Instrument Pair)

**Run**: `alpha-research-20260419-inst-options` | **Round**: R10 | **Status**: prototype (T2 APPROVED)

## Intent

Opportunistic directional trade on the dollar-neutral **TXF-TMF basis**
(residual after 1:20 notional alignment). Entry when basis deviates > 2σ
from its 60-min rolling mean; exit on reversion to mean, 30-min timeout,
or 4σ stop-loss.

Distinct from R7 C66 (passive pair MM, killed at hedge-cost dominance) —
C74 is opportunistic mean-reversion rather than continuous two-sided
quoting. Both legs MAKER by default; TAKER cross only at 4σ stop-loss.

## Mechanism

- `basis(t) = mid_txf(t) - 20 × mid_tmf(t)` — hedge ratio fixed by
  `TXF_pt_value / TMF_pt_value = 200 / 10 = 20` (dollar-neutral).
- Rolling window stats: `mu(t)`, `sigma(t)` over 60-min window.
- **Entry**: `|basis(t) - mu(t)| > 2σ(t)`
  - `basis > mu + 2σ` → SHORT basis: sell-TXF (maker) + buy-TMF (maker)
  - `basis < mu - 2σ` → LONG basis: buy-TXF (maker) + sell-TMF (maker)
- **Exit**: basis crosses mu OR 30-min timeout (both MAKER)
- **Stop-loss**: `|basis - mu_at_entry| > 4 × sigma_at_entry` → TAKER cross
- **Stale-quote filter**: skip entry if `|basis| > 50pt`

## Cost Model

- **Source**: `shared-context.yaml#cost_model` (inst tier, ESTIMATED)
- **Per-trip cost**: TXF RT 1.5pt × 200 + TMF RT 1.5pt × 20 × 10 = **600 NTD/trip**
- **DA T2 cost_drag**: **20%** (very healthy vs C60/C63)
- **requires_broker_confirmation_before_live**: true

## Distinction from killed precedents

- **vs R7 C66 passive pair** (KILLED): C74 is OPPORTUNISTIC (only enters at
  ≥2σ) vs continuous quoting. Avoids hedge-take-cost domination.
- **vs R5 C30 hedge-qty 1× undercount** (KILLED): C74 uses correct 1:20.
- **vs TX-TMF-leadlag** (R26/R28 stay_killed): C74 trades the cointegrated
  residual, not directional lead-lag prediction.
- **vs CBS-mean-reversion** (stay_killed): inter-instrument basis, not
  intra-instrument mid; bid/ask exec not mid.

## Mutual-exclusion constraint

- **MUST NOT** co-deploy with **C63_TXFD6_TIGHT_SPREAD_MAKER** during an
  open C74 trip — TXFD6 inventory conflict (DA T2 flag #9).
- **SHOULD NOT** co-deploy with **C60_TMFD6_SOLO_MAKER** during an open
  C74 trip — TMFD6 inventory conflict.

## Files

- `impl.py` — `TxfTmfBasisMeanReversion` cross-instrument strategy +
  `RollingBasisStats` sliding-window mu/sigma + `C74Alpha` AlphaProtocol.
- `manifest.yaml` — v0.1.0, cites shared-context cost_model, all 10 DA T2
  mandatory T5 flags documented.
- `README.md` — this file.
- `test_alpha.py` — comprehensive test coverage (basis physics, rolling
  stats, entry/exit conditions, stop-loss, stale filter, mutual exclusion,
  AlphaProtocol).
- `__init__.py` — `ALPHA_CLASS = C74Alpha`.

## Key risks for T5

1. **Session-sigma regime asymmetry (S6 WARN)**: DA T2 flagged negative
   direction -5.84 pt/trip vs positive -1.62 pt/trip. T5 must split 2×2
   grid (direction × sigma quartile).
2. **Stale-quote exposure**: basis computation is sensitive to any
   one-sided snapshot; filter must be validated empirically.
3. **Sharpe requires per-trip distribution**, not just daily aggregate.
4. **Adaptive rolling-sigma is MANDATORY** (fixed-sigma would be a
   R14-prior structural-break variant).

## Quick smoke

```python
from research.alphas.c74_txf_tmf_basis_mean_reversion import C74Alpha, C74Params

alpha = C74Alpha(params=C74Params(window_seconds=3600, entry_sigma=2.0))
assert alpha.manifest.instrument == "TXFD6+TMFD6"
assert alpha.strategy.params.hedge_ratio_tmf_per_txf == 20
```
