# c75 Depth-Parity Decision: Drop Deep-Momentum Term (D2) (2026-05-06)

**Author:** Charlie · **Status:** Decision locked · **Successor of:** none

## Problem

c75 (`c75_tmf_mw_ofi_taker`) declares a 3-term composite signal:

```
flow_signal = 0.6 * ofi_l1_ema5s
            + 0.3 * ofi_l1_ema30s
            + 0.1 * deep_depth_momentum_x1000
```

`deep_depth_momentum_x1000` (MLDM, FE-v3 idx 20) is computed by FeatureEngine from L2-L5 depth. **In the backtest, L2-L5 depth never reaches FeatureEngine** because `src/hft_platform/backtest/adapter.py::_build_l1_bidask_event` (line 436) constructs every `BidAskEvent` with only `best_bid` / `best_ask`, even when the source npz contains L5 depth events. So in backtest, `deep_depth_momentum_x1000 == 0` for every tick.

That is Codex finding 5 (parent plan `## Context` item 5). It means the backtest validates `0.6 * ofi_l1_ema5s + 0.3 * ofi_l1_ema30s` and silently drops the third term — i.e. **scores a different alpha than is declared**.

## Two paths

### D1 — Extend the backtest adapter to multi-level

* Modify `_build_l1_bidask_event` (rename `_build_multi_level_bidask_event`) to query hftbacktest's depth API for levels 2-5.
* Concretely: `dp = adapter.hbt.depth(0)`; iterate `dp.bid_qty_at_tick(...)` / `dp.ask_qty_at_tick(...)` for the next 4 ticks (or the API's level accessor; needs verification against installed hftbacktest version).
* Add a fixture proving FeatureEngine's MLDM emits non-zero values.

**Pros:** preserves c75's frozen-weights claim verbatim; closes the live/backtest parity gap for *all* future alphas that consume FE-v3 idx 20.

**Cons:** unknown surface area of the hftbacktest level API; risk of introducing depth-unwinding bugs (DEPTH_CLEAR semantics, partial-fill effects); 2-3x implementation cost vs. D2; could push the c75 verdict another week.

### D2 — Drop the deep-momentum term from c75

* Update `manifest.yaml`: change `dsl_formula` to a 2-term composite; rebalance `parameters.weights` to `{ofi_l1_ema5s: 0.667, ofi_l1_ema30s: 0.333}` (preserves the original 6:3 ratio).
* Update `impl.py`: remove `IDX_DEEP_DEPTH_MOMENTUM_X1000`, `W_DEEP_MOMENTUM`, `deep_mom` reads, and the third term in the composite.
* Update `README.md`: change the formula text and the "Cont/Kukanov OFI 2014 multi-horizon flow lineage" claim — strike "multi-level depth momentum (L2-L5)" from the rationale.
* Update `CHANGELOG.md`: record the change with reference to this runbook.
* Update `tests/test_logic.py`: expected-weight assertions.

**Pros:** small surface area (5 files, ~30 lines); decision is **research-explicit** (the manifest now tells the truth about what's tested); unblocks Step 8 immediately.

**Cons:** changes the alpha; loses the deep-momentum hypothesis from the live/backtest path; if c75 KILLs on `min_sample_size` or `single_day_dominance`, we never validated whether the missing 10% would have moved the verdict.

## Decision: D2

**Why D2:**

1. **Honest scoring beats parameter preservation.** A 3-term alpha scored as a 2-term alpha is a misrepresentation; either we change the adapter (D1) or change the manifest (D2). Either way the artifact must match the test.
2. **Scope discipline.** The dogfood plan ends at Gate D verdict. Adding multi-level adapter work pushes Gate D into a separate sprint and risks scope creep into the wider hftbacktest depth API surface.
3. **The 10% term is not load-bearing for the verdict.** Per `r47_revalidation_2026_04_24.md`, c75-class TMFD6 strategies fail strict gates on `single_day_dominance` and `min_sample_size`, not on the marginal 10% factor. If the 2-term version PROMOTEs to Gate E, we have a clean candidate; we can revisit re-adding deep-momentum **before live deployment** as a Gate-E enhancement, when D1 platform work catches up.
4. **D2 is reversible.** Re-adding the term once D1 lands is a 5-file revert. D1 prematurely committed locks the c75 verdict on a path that hasn't been proven.

## Anti-pattern (parent plan Step 7 callout)

Picking D2 silently and letting README/manifest drift. If D2, the manifest's `hypothesis` and README's "Cont/Kukanov OFI 2014 multi-horizon flow lineage" must be updated to drop the L2-L5 claim. Doc drift makes the next reviewer think the term is still active. **Verification grep:**

```bash
grep -n "deep_depth_momentum_x1000" research/alphas/c75_tmf_mw_ofi_taker/impl.py            # expect 0 hits
grep -n "deep_depth_momentum_x1000" research/alphas/c75_tmf_mw_ofi_taker/manifest.yaml      # expect 0 hits
grep -n "multi-level depth momentum" research/alphas/c75_tmf_mw_ofi_taker/README.md         # expect 0 hits
```

## D1 follow-up (deferred)

If c75 PROMOTEs to Gate E with the 2-term form, file a follow-up under `docs/superpowers/specs/` titled "backtest-adapter-multi-level-depth-parity-2026-XX-XX" covering:

* `_build_multi_level_bidask_event` design.
* hftbacktest depth API discovery (which version, which accessor, how DEPTH_CLEAR interacts).
* Fixture suite proving non-zero MLDM end-to-end.
* Re-adding `deep_depth_momentum_x1000` to c75 and re-running Gate D.

## Verification (post-Step-7-D2)

```bash
uv run pytest research/alphas/c75_tmf_mw_ofi_taker/tests/ -q --no-cov --tb=short
grep -n "deep_depth_momentum_x1000" research/alphas/c75_tmf_mw_ofi_taker/{impl.py,manifest.yaml,README.md}
# expect: only the deprecation note in CHANGELOG.md should mention the dropped term
```

## Cross-references

* npz format zoo: `docs/runbooks/npz-formats-2026-05-06.md`
* TMFD6 corpus: `docs/runbooks/tmfd6-corpus-2026-05-06.md`
* Adapter source: `src/hft_platform/backtest/adapter.py:436` (`_build_l1_bidask_event`)
* Codex adversarial review (2026-05-06): finding 5 (HIGH).
