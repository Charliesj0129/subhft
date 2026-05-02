# C60 — TMFD6 R47-minimal Maker under Institutional RT

**Run**: `alpha-research-20260419-inst-options` | **Round**: R1 | **Status**: prototype

## Intent

Transfer the C33 PROMOTE mechanism (TXFD6 R47-minimal, R7-prior) to TMFD6
under the new institutional-tier cost regime. Retail cost drag 200%
(RT 4 pt / median spread 2 pt) drops to 75% (RT 1.5 pt / median spread 2 pt),
making the same three-layer R47 pattern math-viable on TMFD6.

Empirical CK-direct counterfactual (25 TMFD6 days, qf=1.0, spread>=5,
max_pos=1) yields **+22,451 NTD/day** at inst RT, robust under +/-30% RT
sensitivity (+19,191 to +25,710 NTD/day).

## Mechanism

R47 three-layer pattern:

- **L1 Spread Gate** — `spread >= 5 pt` (fixed; no dynamic per R14-prior)
- **L2 Signal Layers** — D1 PE off, D2 queue off, D3 MFG off; D4 QI skew
  retained (threshold 0.10, widen 1 tick; deployed TMFD6 config).
- **L3 Execution** — fresh-quote requoting, tick-grid snap, fixed 0.2
  ticks/contract inventory skew, max_pos=1 canonical (T5 sweeps {1, 2, 3}).

Non-|pos|-gated — avoids the C22-class meta-kill (skew at max_pos destroys
V-shape). QI skew computed on top-of-book imbalance, NOT on |pos|.

## Cost Model (MANDATORY CITATION)

- **Source**: `outputs/team_artifacts/alpha-research/shared-context.yaml#cost_model.TMF`
- **Tier**: `institutional_estimate` (NOT broker-confirmed)
- **RT**: 1.5 pt (inst estimate) vs retail reference 4 pt
- **Rebate**: 10 NTD/side (MM-designation assumption, ESTIMATED)
- **Tax MM discount**: 50% (already baked into 1.5 pt)
- **PROMOTE flag** (MANDATORY before any live deploy):
  `requires_broker_confirmation_before_live: true`

## Files

- `impl.py` — `TmfD6SoloMakerMinimal` (`MakerStrategy` protocol) +
  `C60Alpha` (`AlphaProtocol` shim); pure-int math; `__slots__`.
- `manifest.yaml` — full manifest including DA T2 carry-forward flags for T5.
- `test_alpha.py` — ports C33 tests + TMFD6-specific boundary + QI-skew tests.
- `__init__.py` — `ALPHA_CLASS = C60Alpha` registry entrypoint.

## Governance

- **T1 Researcher proposal**: `../../../outputs/team_artifacts/alpha-research/round-1/artifacts/researcher_t1_proposal.md`
- **T1 Counterfactual**: `../../../outputs/team_artifacts/alpha-research/round-1/artifacts/researcher_t1_counterfactual_result.md`
- **T2 DA Kill Checklist** (verdict APPROVE): `../../../outputs/team_artifacts/alpha-research/round-1/artifacts/da_t2_kill_checklist.md`

## DA T5 Carry-Forward Requirements

1. Scorecard MUST set `requires_broker_confirmation_before_live: true`.
2. Bid/ask execution MANDATORY (edge margin 4.605 vs 2x spread 4.0 pt narrow).
3. FRESH CK-direct simulation at inst RT 1.5 pt (not linear ex-post re-price).
4. Split scorecard by `max_pos in {1, 2, 3}` (V-shape transfer check).
5. Rebate-off baseline separately; rebate-on is secondary uplift.
6. Apply `hft-backtest-calibration` Common Traps pre-emptively.

## Quick Smoke

```python
from research.alphas.c60_tmfd6_r47_minimal_inst_rt import C60Alpha, C60Params

alpha = C60Alpha(params=C60Params(max_pos=3))
assert alpha.manifest.instrument == "TMFD6"
assert alpha.manifest.strategy_type == "maker"
```

## Live-path wrapper

A `BaseStrategy` live-runtime wrapper (`c60_tmfd6_solo_maker.py`) is NOT in
scope for T4 — it only ships if R1 produces a PROMOTE at T7. When it does,
mirror the `src/hft_platform/strategies/c33_txfd6_solo_maker.py` pattern and
translate x1_000_000 CK scale -> x10_000 platform scale at construction.
