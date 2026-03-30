# R23 Stage 2 — Implementation Review

**Date**: 2026-03-29
**Reviewer**: Execution Reviewer Agent
**Report**: `docs/alpha-research/r23_stage2_rerun_results.md`

---

## Verdict: CONDITIONAL APPROVE — one wiring gap remains

All four implementation components (normalizer, registry, engine, OpMM) are correct in isolation. However, the `on_tick()` method is never called in the live pipeline, which means toxicity will always emit 0 in production.

---

## Findings

### E17: Normalizer Tuple Fix — VERIFIED CORRECT

`normalizer.py:591-604`: `classify()` is now called BEFORE the tuple return in the fast-path. `trade_direction` and `trade_confidence` are appended at positions [8] and [9]. Backward compatible — existing consumers that only access [0]-[7] are unaffected.

Confirmed at 4 call sites per the rerun report (both Rust fast-path and Python fallback for tick normalization).

### E18: Registry Extension — VERIFIED CORRECT

`registry.py:186-192`: `toxicity_ema50_x1000` registered at index [21] with:
- `dtype="i64"`, `scale=1000`, `source_kind="tick"`, `warmup_min_events=50`

Feature set remains `lob_shared_v2` with schema_version=2, now 22 features total. The `source_kind="tick"` annotation is a good signal that this feature depends on tick data (not just LOB stats), which will be useful for future pipeline documentation.

### E19: FeatureEngine Toxicity Kernel — VERIFIED CORRECT (logic)

`engine.py:714-722` (`_compute_toxicity`):
- Guards: `tox_tick_count < 50` (warmup) and `tox_total_vol_ema < 1.0` (convergence). Both sensible.
- Computation: `abs(signed_vol_ema / total_vol_ema) * 1000`, clamped to [0, 1000]. Correct: this measures the absolute imbalance of signed flow, where 0 = balanced and 1000 = fully one-sided.
- Uses `abs()` — the toxicity score is direction-agnostic (high buy pressure and high sell pressure both register as toxic). This is correct for a gate signal.

`engine.py:724-751` (`on_tick`):
- Correctly skips unknown direction and zero volume.
- EMA alpha = 0.04 (≈ 2/(50+1)). Matches the `warmup_min_events=50` in the registry.
- State updates are in-place on `_LobKernelState` — no allocations. Hot-path safe.
- The `ks = self._lob_kernel_states.get(symbol)` guard returns early if no kernel state exists. This means `on_tick()` can only update toxicity for symbols that have already received at least one LOBStatsEvent (which initializes the kernel state via `process_lob_update`). This is correct — we need BBO context before toxicity is meaningful.

`engine.py:528-529`: Toxicity value included in feature tuple at position [21] via `v1_tuple + v2_base + (iss_val, mldm_val, tox_val)`. The old VRR dead code guard (`if n_features <= 21`) has been properly replaced.

### E20: OpMM Toxicity Gate — VERIFIED CORRECT

`opportunistic_mm.py:165-182` (`_check_toxicity_condition`):
- Same pattern as `_check_reversal_condition()`. Permissive when disabled or features unavailable.
- Index constant `_IDX_TOXICITY_EMA50_X1000 = 21` matches registry.
- Length guard `len(features) <= _IDX_TOXICITY_EMA50_X1000` is correct (needs at least 22 elements).
- Threshold comparison: `toxicity > self._toxicity_max_threshold` where default = 700.
- Default disabled (`toxicity_filter_enabled=False`). Safe rollout.
- Called at line 256 in the quoting decision path, alongside the spread gate and reversal filter.

### E21: CRITICAL — `on_tick()` Not Called in Live Pipeline

**`FeatureEngine.on_tick()` has no caller in the runtime pipeline.**

Grep for `feature_engine.on_tick` or `_feature_engine.on_tick` across `src/hft_platform/` returns zero results (only the method definition itself and the strategy's unrelated `on_tick`).

In `services/market_data.py:_process_raw()` (lines 612-693):
1. Raw message is normalized → TickEvent or BidAskEvent
2. `self.lob.process_event(event)` → returns stats (or None)
3. `self._maybe_update_features(event, stats)` → calls `process_lob_update()`

Step 3 only calls `process_lob_update()` — it does NOT call `on_tick()`. For TickEvents, the classification data (`trade_direction`, `trade_confidence`) is available on the event object but never forwarded to the FeatureEngine.

**Impact**: In production, `tox_tick_count` will remain 0, `_compute_toxicity()` will always return 0, and the toxicity feature at [21] will be permanently zero. The OpMM toxicity gate will always return True (permissive) since 0 < 700.

**Fix** (~5 LOC in `services/market_data.py`): In `_maybe_update_features()` or `_process_raw()`, add:

```python
if isinstance(event, TickEvent) and self.feature_engine is not None:
    self.feature_engine.on_tick(
        event.symbol, event.price, event.volume,
        event.trade_direction, event.trade_confidence,
    )
```

This should go BEFORE the `process_lob_update()` call so that toxicity state is updated before the feature tuple is emitted.

For the tuple fast-path, the equivalent would extract fields from tuple positions [1], [2], [3], [8], [9].

### E22: Rerun Results — EXPECTED (bit-for-bit identical)

The rerun report correctly notes results are identical because the diagnostic script uses its own offline TradeClassifier instance replaying .npy data. The pipeline changes (normalizer tuple fix, FE on_tick, OpMM gate) only affect the live path, not offline replay. No concerns here.

### E23: Edge Case — Volume=1 Guard Threshold

The rerun report notes that with volume=1 trades, `tox_total_vol_ema` converges to ~0.91, below the 1.0 guard threshold. This means toxicity emits 0 for very low-volume symbols.

This is acceptable for TAIFEX futures where lot size >= 1 and trade frequency is high enough that the EMA converges above 1.0 quickly. However, for completeness:
- TMFD6: ~1M ticks/day, volume typically >= 1. EMA converges above 1.0 within ~25 ticks. No issue.
- TXFD6: ~500K ticks/day, same. No issue.
- Theoretical edge: a symbol with exclusively volume=1 trades at very low frequency. The guard prevents division-by-very-small-number artifacts. Acceptable trade-off.

### E24: Test Coverage

158 tests pass with no regressions. The rerun report mentions a unit test for the `on_tick()` → `_compute_toxicity()` → feature tuple pipeline (60 warmup LOBStats + 80 BUY ticks). This is a minimal happy-path test.

Missing test coverage (non-blocking but recommended):
- Mixed BUY/SELL flow producing intermediate toxicity values
- Reset behavior (`reset_symbol()` should zero toxicity state)
- Toxicity behavior when `on_tick()` is called without prior `process_lob_update()` (should no-op due to missing kernel state)
- OpMM `_check_toxicity_condition()` with features shorter than 22 elements (should return True)

---

## Summary

| Item | Status | Blocking? |
|------|--------|-----------|
| E17: Normalizer tuple fix | CORRECT | No |
| E18: Registry extension | CORRECT | No |
| E19: Toxicity kernel | CORRECT (logic) | No |
| E20: OpMM gate | CORRECT | No |
| E21: `on_tick()` not wired | **MISSING** | **YES** |
| E22: Rerun identical | Expected | No |
| E23: Volume=1 edge case | Acceptable | No |
| E24: Test coverage | Minimal, adequate | No |

**Overall**: CONDITIONAL APPROVE. The implementation is correct but incomplete — `on_tick()` must be wired into `MarketDataService._process_raw()` or `_maybe_update_features()` (~5 LOC). Without this, toxicity will always be zero in production.

Once the wiring is added, Stage 2 implementation is complete and ready for shadow deployment.
