# Round 18 Stage 2b Execution Review: MLOFI Microprice Correction (Bug-Fixed Reanalysis)

**Reviewer**: Execution Reviewer
**Date**: 2026-03-27
**Artifacts reviewed**:
- `docs/alpha-research/round18_stage2b_mlofi_microprice.md` (results)
- `research/alphas/mlofi_microprice/impl.py` (fixed alpha implementation)
- `research/alphas/mlofi_microprice/backtest_ic.py` (fixed IC analysis + 5 new challenge functions)

**Prior review**: `docs/alpha-research/round18_stage2_execution_review_mlofi_microprice.md` (Stage 2 REJECT)

**Overall verdict**: **APPROVE TERMINATE**

---

## 1. Bug Fix Verification

### Bug 1: Hot-Path Heap Allocation in impl.py — FIXED

**Prior issue**: Lines 147-150 allocated `np.zeros(_N_LEVELS, dtype=np.int64)` on every `update()` call for `cur_bp`/`cur_ap`.

**Fix verification**: `__init__` now pre-allocates `_cur_bid_price` and `_cur_ask_price` as instance attributes (lines 112-115). Both are in `__slots__` (lines 92-93). `update()` uses `cur_bp[:] = 0` / `cur_ap[:] = 0` (lines 153-154) for in-place zeroing. `reset()` also zeroes them in-place (lines 208-211).

**Verdict**: FIXED correctly. No heap allocation on the hot path. Allocator Law compliant.

### Bug 2: valid mask always True after np.clip — FIXED

**Prior issue**: `backtest_ic.py` line 187: `valid = future_idx < n` was computed AFTER `np.clip(future_idx, 0, n-1)`, making it trivially True.

**Fix verification**: Lines 183-187 now compute `valid = future_idx_raw < n` BEFORE clipping:
```python
future_idx_raw = np.searchsorted(ts_ns, target_ts, side="left")
valid = future_idx_raw < n       # computed from RAW index
future_idx = np.clip(future_idx_raw, 0, n - 1)  # then clip for safe indexing
```

The `too_far` filter at lines 190-194 provides a second defense. As the artifact notes, B2 impact is minimal because `too_far` already caught most OOB cases, but the fix is structurally correct — the valid mask now has the semantics its name implies.

**Verdict**: FIXED correctly. The same fix is applied consistently in `spearman_ic_nonoverlap` (line 611-613) and `compute_realized_spread_per_fill` (line 547-549).

### Bug 3: Realized Spread Direction — FIXED

**Prior issue**: Line 562 used `-direction * (mid_future - mid_at_fill)` assuming contrarian positioning, but MLOFI is pro-cyclical on TWSE.

**Fix verification**: Lines 557-562 now use pro-cyclical direction:
```python
direction = np.sign(mlofi[fill_mask])  # positive MLOFI = BUY
realized_spread = direction * (mid_future - mid_at_fill)
```

Comment on line 559-561 explains the pro-cyclical interpretation correctly. All 17 days now show positive mean realized spread (~+4,340 units, 0.434 NTD), confirming the sign correction was needed.

The median=0 on all days is an important caveat correctly noted in the artifact: the majority of threshold-0.5 "fills" see no mid-price movement at 30s. The positive mean is driven by tail moves in the signal direction — consistent with trend-following (which is the Stage 2b conclusion).

**Verdict**: FIXED correctly.

---

## 2. Config Drift Disposition

### Drift #1: Feature Name Mismatch — RESOLVED (Stage 2)

`mlofi_gradient_x1000` does not exist; correct name is `deep_depth_momentum_x1000` at index 20. Stage 2 impl uses `feature_set_version="lob_shared_v2"` reference without proposing a new feature index. No change in Stage 2b. Remains RESOLVED.

### Drift #2: MM Strategies Don't Consume Microprice — RESOLVED via TERMINATE

The Stage 2 review flagged that no strategy consumes an MLOFI microprice correction term. Stage 2b concludes TERMINATE for the entire MLOFI microprice alpha: the signal is redundant trend-following (detrended IC = -0.032 at 30s), and L1 alone outperforms the multi-level composite (L1 IC = +0.217 vs full MLOFI IC = +0.206). There is no alpha to integrate. The consumption gap is moot — there is nothing worth consuming.

**Status**: RESOLVED (subsumed by TERMINATE). No future integration work needed.

### Drift #3: Cross-Symbol Propagation — RESOLVED via TERMINATE

The Stage 2 review flagged that no cross-symbol bus exists for 2330 MLOFI to influence TXFD6/TMFD6 quoting. Stage 2b confirms TXFD6 is dead (IC near zero, wrong sign, CV=337%) and the 2330 signal is trend-following, not microstructure alpha. Cross-symbol propagation of a redundant trend indicator adds no value.

**Status**: RESOLVED (subsumed by TERMINATE). No infrastructure work needed.

### Config Drift Summary Table

| # | Drift | Stage 2 Status | Stage 2b Status | Resolution |
|---|-------|----------------|-----------------|------------|
| 1 | Feature name mismatch (`mlofi_gradient_x1000` vs `deep_depth_momentum_x1000`) | RESOLVED | RESOLVED | Correct `lob_shared_v2` reference; no new index |
| 2 | MM strategies don't consume microprice | UNRESOLVED | **RESOLVED** | Subsumed by TERMINATE — no alpha to integrate |
| 3 | Cross-symbol propagation not supported | UNRESOLVED | **RESOLVED** | Subsumed by TERMINATE — no value in propagation |

**Unresolved drifts: 0**

---

## 3. New Analysis Functions — Correctness Assessment

### 3.1 Non-Overlapping IC (`spearman_ic_nonoverlap`, lines 576-631)

**Method**: Walk through timestamps, selecting the next observation only when `ts_ns[i] >= next_ts` where `next_ts` is the previous sample's timestamp + horizon. This ensures zero overlap between consecutive forward return windows.

**Correctness check**:
- Subsampling starts at `WARMUP_TICKS` (line 595) — correct, avoids zero-signal region.
- Forward return uses the same `searchsorted` + `valid` + `too_far` pattern as the main function — consistent.
- `valid = future_idx_raw < n` computed before clipping (line 612) — Bug 2 fix applied here too.
- Minimum 30 observations required (line 603, 627) — reasonable floor.

**Potential concern**: The subsampling is deterministic (starts from the first valid tick). Different starting offsets could yield slightly different ICs. However, with ~550 observations per day at 30s horizon, the subsampling is dense enough that start-point sensitivity should be negligible. Not a blocking issue.

**Verdict**: Correct implementation. The 35% IC drop (0.206 -> 0.134 at 30s) is in the expected range for overlapping-to-non-overlapping correction on highly autocorrelated tick data.

### 3.2 Detrended IC (`compute_ic_detrended`, lines 638-693)

**Method**: Partition the trading day into 5-minute bins. Within each bin, subtract the local mean of forward returns from all ticks in that bin. Then compute Spearman IC on the detrended returns.

**Correctness check**:
- Time-based binning: `bin_idx = ((ts_ns - t0) // window_ns).astype(np.int64)` (line 666). Correct integer division for non-overlapping bins.
- Local mean uses `np.nanmean(fwd_ret[combined])` where `combined = mask_bin & valid_mask` (lines 671-674). Correctly handles NaN and restricts to valid observations.
- `valid_mask` includes `signal != 0.0` filter (line 654), ensuring warmup zeros are excluded from local mean computation.
- Subtraction changes RANKS (unlike constant subtraction from the whole day), which is the key insight enabling Spearman IC to detect trend contamination.

**Potential concern**: The bin-edge effect — ticks at the boundary of a 5-minute window have their forward return partially in the next window's trend. This is inherent to any windowed detrending and does not invalidate the approach. The 5-minute window (300s) is large relative to the 30s horizon, so most forward returns fall within the same bin.

**Verdict**: Correct implementation. The result (detrended IC = -0.032 at 30s, flipping to negative at all horizons below 60s) is the critical finding that disproves the alpha hypothesis.

### 3.3 L1-Only MLOFI (`compute_mlofi_l1_only`, line 700-707)

Delegates to `compute_mlofi_integrated(..., lam=0.0)`. With `lam=0.0`, weights become `[0**0, 0**1, 0**2, 0**3, 0**4] = [1, 0, 0, 0, 0]`. This is correct — Python evaluates `0**0 = 1`. Pure L1 delta OFI with EMA smoothing.

### 3.4 Deep-Only MLOFI (`compute_mlofi_deep_only`, lines 710-751)

Manually constructs weights `[0.0, lam, lam^2, lam^3, lam^4]` (line 722). Correctly excludes L1 by setting weight[0] = 0. Uses the same BBO-shift guard and level-shift guard as the main function. EMA smoothing is identical.

**Verdict**: Both decomposition functions are correct.

### 3.5 OOS Incremental IC (`compute_oos_incremental_ic`, lines 758-806)

**Method**: Process days chronologically. For day t, use day t-1's regression alpha coefficient. Skip day 0 (no prior coefficient).

**Correctness check**:
- `prev_alpha` initialized to `None` (line 773). Day 0 produces `today_alpha` but does not compute OOS IC. Correct — no look-ahead.
- Day 1+ uses `prev_alpha * mlofi` as the correction term, then computes incremental IC against `residual_ret = fwd_rets - l1_pred` (lines 792-798). The L1 prediction baseline is recomputed per-day (no leakage).
- The regression alpha is learned entirely on day t-1's data and applied to day t's signal. This is genuine OOS.

**Verdict**: Correct implementation. The OOS incremental IC (+0.119 at 30s) being nearly identical to in-sample (+0.120) indicates the regression coefficient is stable day-to-day. However, as the artifact correctly notes, this incremental IC is also trend-driven.

---

## 4. Assessment of the TERMINATE Recommendation

The TERMINATE case rests on four pillars:

1. **Detrended IC flips negative** (IC = -0.032 at 30s after removing 5-min local trends). This is the strongest evidence. If removing the daily trend component destroys (and inverts) the signal, the "alpha" is just lagged momentum. The NW t-stat of -3.95 means this inversion is statistically significant.

2. **L1 alone outperforms full MLOFI** (L1 IC = +0.217 vs full MLOFI IC = +0.206 at 30s). The multi-level (L2-L5) contribution is IC = +0.029, which is marginal. The "multi-level value-add" narrative from Stage 2a is disproved.

3. **IC monotonically increases with horizon** (0.093 at 250ms -> 0.212 at 60s). This is a trend-following signature, not a microstructure signal. Genuine microstructure alpha peaks at a specific timescale and decays.

4. **Overlapping inflation is 35%** (0.206 overlapping vs 0.134 non-overlapping at 30s). While the non-overlapping IC still passes the kill gate (0.134 >> 0.015), combined with pillar 1, this is trend-capture, not alpha.

Each pillar individually could be challenged. Together, they form an airtight case. The signal is a slow EMA that correlates with the local drift. It is not microstructure information.

**TXFD6**: Already dead from Stage 2 (IC near zero, wrong sign, CV=337%). Stage 2b conditional L5 analysis confirms no signal even with full L5 coverage.

---

## 5. Residual Concerns (Non-Blocking)

### 5.1 Realized Spread Interpretation

The corrected realized spread (+4,340 units mean, 0 median) tells the same story as the detrended IC: the signal captures tail moves in the trend direction (positive mean) but the majority of observations show zero price movement at 30s (zero median). This is consistent with trend-following rather than microstructure prediction.

### 5.2 impl.py Docstring Staleness

The impl.py module docstring (lines 17-23) still contains Stage 2a claims: "Genuine multi-level value-add" and "Incremental IC over L1 = +0.12 at 30s". These are disproved by Stage 2b. Since the recommendation is TERMINATE, updating the docstring is low priority, but it should be noted for completeness.

### 5.3 print() Usage in backtest_ic.py

`backtest_ic.py` uses `print()` throughout (e.g., lines 298-304, 339, 346). Per project coding style, `structlog` is preferred. However, this is a research CLI script (`__main__` entry point), not hot-path production code. Non-blocking per testing rule "Skip: CLI glue code."

---

## 6. Final Verdict

### APPROVE TERMINATE

All three bugs are properly fixed. All three config drifts are resolved (drift #1 from Stage 2, drifts #2 and #3 subsumed by TERMINATE). The five new analysis functions are correctly implemented and their results conclusively demonstrate that the MLOFI microprice correction is redundant trend-following, not microstructure alpha.

The TERMINATE recommendation is sound from an execution perspective. No further work is warranted on this alpha direction.

**Disposition**: Close `mlofi_microprice_correction` research line. The L1 imbalance finding (IC = +0.217 at 30s) is already captured by the existing `ofi_l1` feature in FeatureEngine. No new features, strategies, or infrastructure changes are required.
