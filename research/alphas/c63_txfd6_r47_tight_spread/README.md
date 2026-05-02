# C63 — TXFD6 R47-minimal with Tightened Spread Threshold

**Run**: `alpha-research-20260419-inst-options` | **Round**: R2 | **Status**: prototype

## Intent

Single-lever variant of C33 (TXFD6 R47-minimal PROMOTE, R7-prior):
lowers `spread_threshold_pts` from 5 to 3 while keeping mp=3,
queue_share=0.05, R47-minimal (all four signal layers DISABLED),
non-|pos|-gated. Institutional-tier TXF RT 1.5 pt (halves retail 3 pt)
structurally re-prices the 3 pt spread band from break-even to +1.5 pt
margin per cycle.

## Mechanism (R47 three-layer, C33 precedent)

- **L1 Spread Gate** — `spread >= 3 pt` (LOWERED from C33's 5;
  structural-break-justified, not session-adaptive)
- **L2 Signal Layers** — all four (PE / Queue / MFG / QI) DISABLED.
  TXFD6 is R47-minimal per C33/R7 T1 counterfactual (TMFD6-calibrated
  layers do not transfer).
- **L3 Execution** — fresh-quote requoting, tick-grid snap, fixed 0.2
  ticks/contract inventory skew (LINEAR in pos; NOT |pos|-gated),
  max_pos=3 canonical.

R14-prior structural-break guard: threshold change is justified by cost
regime (inst RT 1.5 pt vs retail 3 pt), not TOD/session variance.

## Cost Model (MANDATORY CITATION)

- **Source**: `outputs/team_artifacts/alpha-research/shared-context.yaml#cost_model.TXF`
- **Tier**: `institutional_estimate` (NOT broker-confirmed)
- **RT**: 1.5 pt (inst estimate) vs retail reference 3 pt
- **Point value**: 200 NTD/pt (TXF, NOT TMF's 10)
- **PROMOTE flag** (MANDATORY before any live deploy):
  `requires_broker_confirmation_before_live: true`

## Files

- `impl.py` — `TxfD6R47TightSpreadMaker` (`MakerStrategy` protocol) +
  `C63Alpha` (`AlphaProtocol` shim); pure-int math; `__slots__`.
- `manifest.yaml` — full manifest with distinction_from_killed_classes
  (C33 precedent, C41-prior, R16, C22-class).
- `test_alpha.py` — spread-gate boundaries (sp=1/2/3/4/5/6), all four
  D1-D4 signal layer absence, max_pos {1,3,5}, inventory skew, bid/ask
  execution, price-move gate, on_gap, AlphaProtocol.
- `__init__.py` — `ALPHA_CLASS = C63Alpha` registry entrypoint.

## Quick Smoke

```python
from research.alphas.c63_txfd6_r47_tight_spread import C63Alpha, C63Params

alpha = C63Alpha(params=C63Params(max_pos=3))
assert alpha.manifest.instrument == "TXFD6"
assert alpha.manifest.strategy_type == "maker"
```

## Governance

- **T1 Researcher proposal**: `../../../outputs/team_artifacts/alpha-research/round-2/artifacts/researcher_t1_proposal.md` (pending)
- **T2 DA Kill Checklist**: `../../../outputs/team_artifacts/alpha-research/round-2/artifacts/da_t2_kill_checklist.md` (pending)

## Live-path wrapper

A `BaseStrategy` live-runtime wrapper (`c63_txfd6_solo_maker.py`) is NOT
in scope for T4 — it only ships if R2 produces a PROMOTE at T7. If it
does, mirror the `src/hft_platform/strategies/c33_txfd6_solo_maker.py`
pattern (same instrument, same scale, only threshold=3 change).
