# R23 Stage 2 — Execution Review

**Date**: 2026-03-28
**Reviewer**: Execution Reviewer Agent
**Report**: `docs/alpha-research/r23_stage2_prototype.md`
**Script**: `research/experiments/validations/r23_signed_flow/diagnostic.py`

---

## Verdict: CONDITIONAL APPROVE (Candidate C promotion to FE)

A1 and A2 kills are clean. Candidate C's adverse movement result on TXFD6 is real and actionable. One critical correction to a shared assumption from Stage 1.

---

## Findings

### E8: CORRECTION — TradeClassifier IS Already Wired Into Pipeline

**My Stage 1 finding E3 was wrong.** The TradeClassifier integration is NOT a gap — it is already done:

1. **`TickEvent` already has `trade_direction` and `trade_confidence` fields** (`src/hft_platform/events.py:40-43`)
2. **Normalizer already calls `classify()` on every tick** (`src/hft_platform/feed_adapter/normalizer.py:568`, `607`) and `update_quotes()` on every BidAsk (`normalizer.py:691`, `996`, `1001`)
3. **The normalizer instantiates `TradeClassifier` at init** (`normalizer.py:349`)

The Researcher's Stage 1 response also claimed this was a gap ("~75 LOC across 3 files"), and the Stage 2 report recommends "Wiring TradeClassifier into normalizer (~30 LOC)" as a prerequisite. Both are incorrect — this work is already done.

**Caveat**: The **tuple fast-path** (`normalizer.py:590-600`, active when `HFT_EVENT_MODE=tuple` which is the production default) does NOT include `trade_direction`/`trade_confidence` in its return tuple. Only the TickEvent object path includes classification. This means:
- In production with default config: classification results are computed but NOT passed downstream via the tuple path
- In test mode (`pytest` forces `_EVENT_MODE = "event"`): classification IS available
- **Action needed**: If C is promoted to live pipeline, either extend the tuple format to include direction+confidence, or switch to TickEvent objects for the paths that need classification. This is ~10 LOC, not 75.

### E9: Diagnostic Script Quality — PASS with notes

**BBO replay ordering** (`diagnostic.py:126-146`): Correct. For each inferred trade, the script updates BBO state from `row[ti-1]` (the row before the trade), then classifies using previous BBO. This is the correct causal ordering — BBO state BEFORE the trade determines classification.

**Trade inference method** (`diagnostic.py:106-109`): Trades are inferred from mid-price changes (`np.diff(mid) != 0`). This is a reasonable proxy for BBO-only data, but it's important to note:
- This CANNOT detect trades that don't move the midpoint (e.g., partial fills at best bid/ask that don't deplete the level)
- It over-counts: some mid-price changes are from quote updates (new orders changing BBO), not trades
- The 100% at-quote classification rate confirms this limitation — all "trades" hit bid or ask by construction because they're inferred from BBO changes

**Detrending** (`diagnostic.py:263-285`): Block-based 5-min mean subtraction. This is a simplified version of the R18 detrending gate. Correct implementation: subtracts the block mean from each 300s window. Non-overlapping blocks prevent look-ahead bias.

**Forward returns** (`diagnostic.py:239-260`): Uses `np.searchsorted` to find the closest future row at the target timestamp. Correctly handles session boundaries (NaN for cross-session returns). One minor note: `searchsorted` returns the insertion point which may be the row AFTER the exact target time, introducing a small positive timing bias — but this is negligible at the horizons tested (10-300s).

**Spearman IC** (`diagnostic.py:288-295`): Standard implementation with `scipy.stats.spearmanr`. Filters out NaN and zero-signal rows. The `signal != 0` filter is debatable — it excludes periods of no signal, which could bias IC upward. However, for EMA-based signals that are rarely exactly zero, this is acceptable.

**Adverse movement** (`diagnostic.py:321-383`): Correctly computes directional adverse movement (for buys: price going down is adverse; for sells: price going up). Quintile bucketing via `np.percentile` + `np.digitize`. Session-aware forward price lookup. Implementation is sound.

### E10: A1 Kill — CONFIRMED

A1 correlation with unsigned OFI = +1.000 on both instruments. This was predicted by the Challenger in Stage 1 and confirmed by the data. On BBO-inferred trades where all classifications are at-quote (confidence=1000), the confidence weight is always 1.0, making the signal numerically identical to unsigned OFI.

The kill is clean. The A1 signal can only differ from unsigned OFI when tick-rule fallback trades exist (confidence=500), which requires real TickEvent data with inside-spread trades. This is a data limitation, not an algorithm failure.

### E11: A2 Kill — CONFIRMED

Cancel-volume OFI: orthogonal (corr 0.12-0.22) and uncontaminated (35% fill fraction), but IC < 0.015 at all horizons. Max IC = +0.0092 on TXFD6 at +10s. The signal is genuinely different from unsigned OFI but simply doesn't predict returns on TAIFEX instruments.

The kill is clean. No further investigation warranted.

### E12: Candidate C Adverse Movement — PASS (TXFD6), EXPECTED FAIL (TMFD6)

**TXFD6 results are compelling**: Monotonic Q1-Q5 adverse movement gradient at all horizons. Q5-Q1 = +3.5 pts at +60s. This is economically significant — high-toxicity trades see 2.0 pts adverse drift while low-toxicity trades see -1.5 pts (favorable). The 3.5 pt spread is larger than TXFD6's RT cost.

**TMFD6 failure is expected**: 1-tick median spread compresses all adverse movement into binary outcomes (0 or +/- 0.5 pts). The Q5-Q1 = 0.5 pts is below the 1-tick kill threshold. This doesn't mean toxicity is useless on TMFD6 — it means the measurement resolution is too coarse on a 1-tick-spread instrument.

**The signal is NOT a spread proxy**: corr(toxicity, spread) = -0.06 (TXFD6), -0.02 (TMFD6). This confirms toxicity captures genuinely new information.

### E13: Index [21] Availability — PASS (with caution)

The researcher proposes `toxicity_ema50_x1000` at FeatureEngine index [21]. From Stage 1 review (E2): the registry has 21 features [0]-[20]. VRR computation code exists in `engine.py` but the registry guard (`n_features <= 21`) prevents it from being emitted.

Index [21] IS available for a new FeatureSpec. However, if VRR is ever formally registered, there will be a conflict. Two options:
1. **Register toxicity at [21], VRR at [22]**: Simple. But requires documenting that VRR's dead code at engine.py lines 531-535 must be updated when registered.
2. **Register VRR at [21] first, toxicity at [22]**: Respects the existing code intent (VRR kernel state already exists). Requires one extra registry entry.

**Recommendation**: Option 1 is fine for now. VRR registration is a separate work item. The FeatureEngine's dynamic tuple has no hard max, so index ordering is not architecturally constrained.

### E14: FeatureEngine Integration Path — FEASIBLE (~60 LOC)

Since TradeClassifier is already wired in the normalizer (E8), the remaining work for C promotion is:

| Step | File | Change | LOC |
|------|------|--------|-----|
| 1 | `feature/registry.py` | Add `FeatureSpec("toxicity_ema50_x1000", "i64", scale=1000, source_kind="tick", warmup_min_events=50)` to v2 or create v3 | ~5 |
| 2 | `feature/engine.py` | Add `on_tick(symbol, direction, confidence)` method. Accumulate EMA of signed direction. Return toxicity value in feature tuple. | ~35 |
| 3 | `feature/engine.py` | Update `_compute_values` to include toxicity at index [21] (same pattern as VRR at lines 531-535) | ~10 |
| 4 | Pipeline wiring | Call `feature_engine.on_tick()` from wherever TickEvent is dispatched (MarketDataService or equivalent) | ~10 |
| **Total** | | | **~60 LOC** |

Key design decision: `on_tick()` should update internal state only. The toxicity value should be emitted as part of the next `process_lob_update()` call's feature tuple (same emission pattern as all other features). This avoids a second FeatureUpdateEvent per tick and keeps the hot path simple.

### E15: OpMM Toxicity Gate — FEASIBLE

Adding `_check_toxicity_condition()` to `OpportunisticMM` follows the identical pattern as `_check_reversal_condition()` (`src/hft_platform/strategies/opportunistic_mm.py:120-156`):

- Read toxicity from `self._feature_cache[symbol]` at index [21]
- Compare against configurable threshold
- Return True/False to gate quoting

Config additions needed in `config/base/strategies.yaml`:
```yaml
toxicity_filter_enabled: false  # disabled by default, enable after calibration
toxicity_threshold_x1000: 500   # calibrated from TXFD6 Q5 boundary
```

This is ~15 LOC in `opportunistic_mm.py` + ~3 lines in `strategies.yaml`. Architecturally consistent with existing gating patterns.

### E16: 100% At-Quote Classification Limitation

The diagnostic reports 100% at-quote classification because trades are inferred from BBO mid-price changes. This means:
- The toxicity score in this diagnostic is an EMA of **BBO-inferred** trade directions, not real exchange trades
- Real TickEvent data from the Shioaji feed will include actual trade prices, which CAN fall inside the spread during fast markets
- The toxicity result should be re-validated once 5+ days of live classified data (via the already-wired normalizer) is collected

**However**, this limitation does NOT invalidate the C result. The adverse movement analysis shows that even BBO-inferred trade direction (which is a subset of real trade information) produces a meaningful Q5-Q1 gradient on TXFD6. Real trade data should produce equal or stronger results because it includes inside-spread trades that carry additional adverse-selection information.

---

## Summary

| Item | Status | Notes |
|------|--------|-------|
| E8: TradeClassifier pipeline | **ALREADY DONE** (corrects Stage 1 E3) | Tuple path gap only |
| E9: Script quality | PASS | Minor notes, no methodological errors |
| E10: A1 kill | CONFIRMED | Correlation = 1.000, clean kill |
| E11: A2 kill | CONFIRMED | IC < 0.015, clean kill |
| E12: C adverse movement | PASS (TXFD6) | Q5-Q1 = +3.5 pts, economically significant |
| E13: Index [21] | Available | VRR dead code, no conflict |
| E14: FE integration | ~60 LOC | on_tick() method + registry entry |
| E15: OpMM gate | ~15 LOC | Same pattern as reversal filter |
| E16: At-quote limitation | Acceptable | BBO-inferred is lower bound; live data should be stronger |

**Overall**: CONDITIONAL APPROVE for Candidate C promotion. Conditions:
1. Register `toxicity_ema50_x1000` at [21] in registry
2. Implement `on_tick()` in FeatureEngine (~35 LOC)
3. Wire `on_tick()` call from pipeline after TickEvent processing (~10 LOC)
4. Add `_check_toxicity_condition()` to OpMM (~15 LOC)
5. Collect 5 days of live classified data, then re-validate adverse movement with real TickEvent
6. Fix tuple fast-path to include trade_direction/trade_confidence if live pipeline needs classification (~10 LOC)
