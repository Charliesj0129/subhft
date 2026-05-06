# c75_tmf_mw_ofi_taker — Changelog

## 0.2.0 — 2026-05-06 (D2: drop deep-momentum term)

**Source:** `docs/runbooks/c75-depth-parity-decision-2026-05-06.md`,
Codex adversarial-review 2026-05-06 finding 5 (HIGH).

### Changed

- **Composite reduced from 3-term to 2-term.** Dropped
  `0.1 * deep_depth_momentum_x1000` (FE-v3 idx 20).  Reason: the backtest
  adapter's `_build_l1_bidask_event` (`src/hft_platform/backtest/adapter.py:436`)
  emits only L1 quotes, so MLDM collapses to zero in Gate C — i.e. the
  3-term backtest was secretly scoring a 2-term strategy. Either fix the
  adapter (D1) or drop the term (D2). D2 chosen for scope discipline; D1
  is filed as deferred follow-up under `docs/superpowers/specs/`.
- **Weights renormalised from 0.6 / 0.3 to 0.667 / 0.333** (preserves the
  original 6:3 short/long-window ratio).
- `manifest.yaml::dsl_formula`, `manifest.yaml::parameters.weights`,
  `manifest.yaml::hypothesis`, `manifest.yaml::formula` updated to
  match the 2-term form.
- `impl.py`: removed `IDX_DEEP_DEPTH_MOMENTUM_X1000`, `W_DEEP_MOMENTUM`,
  and the third term in the composite.
- `tests/test_logic.py`: regenerated for 2-term weights.

### Why not D1?

D1 (extend `_build_l1_bidask_event` to L2-L5) is the preserve-the-alpha
path but has unknown surface area against the installed `hftbacktest`
version's depth API and risks DEPTH_CLEAR semantics bugs. D2 is the
ship-the-honest-version path: 5-file edit, manifest now matches what the
test scores. If the 2-term version PROMOTEs to Gate E, the deferred D1
follow-up can re-add the deep-momentum term as a Gate-E enhancement.

## 0.1.0 — 2026-05-06 (initial draft, 3-term)

Original draft authored under `~/.claude/plans/reflective-marinating-brooks.md`
with weights 0.6 / 0.3 / 0.1 over (ema5s, ema30s, deep_depth_momentum_x1000).
Never executed a real backtest -- scorecard.json was an all-zero
scaffold pending the full Gate A->D run that this plan delivers.
