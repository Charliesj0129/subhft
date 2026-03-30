# Round 18 Stage 2 Execution Review: MLOFI Microprice Correction

**Reviewer**: Execution Reviewer
**Date**: 2026-03-27
**Artifacts reviewed**:
- `docs/alpha-research/round18_stage2_mlofi_microprice.md` (results)
- `research/alphas/mlofi_microprice/impl.py` (alpha implementation)
- `research/alphas/mlofi_microprice/backtest_ic.py` (IC analysis)

**Overall verdict**: **REJECT** (2 unresolved config drifts + 2 code bugs)

---

## 1. Code Correctness: impl.py

### 1.1 Pattern Compliance

The implementation follows the standard alpha pattern (`AlphaManifest`, `update()` -> `int`, `reset()`, `get_signal()`, `ALPHA_CLASS`). Uses `__slots__` correctly. Pre-allocates `_prev_bid_qty`, `_prev_ask_qty`, `_cur_bid_qty`, `_cur_ask_qty` as contiguous numpy arrays in `__init__`. EMA window=8 matches FeatureEngine convention.

### 1.2 MLOFI Delta Computation

Correct. Line 164-166: `delta_bid = cur_bq - self._prev_bid_qty` computes CHANGES in depth, not absolute levels. OFI = delta_bid - delta_ask (level-by-level). This follows the Cont/Cucuringu/Zhang (2023) formulation.

### 1.3 Geometric Weighting

Correct. Line 113: `[lam ** k for k in range(_N_LEVELS)]` yields `[1, lam, lam^2, lam^3, lam^4]` for levels 1-5, matching the formula `w_k = lambda^(k-1)`. Consistent between `impl.py` and `backtest_ic.py`.

### 1.4 BBO-Shift Guard

Correct. Lines 155-161: zeros MLOFI when best bid or ask price changes. Also zeros individual levels where that level's price changed (lines 169-171). This is the right conservative approach to avoid conflating price-level shifts with genuine depth changes.

### 1.5 Warmup Handling

Correct. Returns 0 for first 64 ticks (line 187-188). First tick initializes `_prev_*` arrays without computing OFI (line 160-161, 181-183).

### 1.6 BUG: Hot-Path Heap Allocation (MODERATE)

**Lines 147-150**: `cur_bp = np.zeros(_N_LEVELS, dtype=np.int64)` and `cur_ap = np.zeros(...)` allocate new numpy arrays on every `update()` call. This violates the Allocator Law. The quantity arrays (`_cur_bid_qty`, `_cur_ask_qty`) are correctly pre-allocated in `__init__`, but the price arrays are not.

**Fix**: Add `_cur_bid_price` and `_cur_ask_price` to `__slots__` and pre-allocate in `__init__`, then use `self._cur_bid_price[:] = 0` in `update()` instead of creating new arrays.

This is classified MODERATE because the alpha is research-only (not hot-path production code), but would be a blocker for any FeatureEngine v3 promotion.

### 1.7 Off-By-One Check

No off-by-one errors found. Level indexing is 0-based (`range(_N_LEVELS)` = 0..4), matching numpy array indexing for L1..L5. `bids[:n_bid, 1]` correctly extracts volume column (column 1) from shape (N, 2) data.

---

## 2. Code Correctness: backtest_ic.py

### 2.1 Forward Return Computation

Correct. Line 188: `fwd[valid] = mid[future_idx[valid]] - mid[valid]` computes `mid(t+T) - mid(t)`. Uses `searchsorted` for efficient nearest-tick lookup. NaN tolerance filter (lines 192-194) removes entries where the closest future tick is more than 2x the horizon away.

Minor note: After `np.clip(future_idx, 0, n-1)`, the check `valid = future_idx < n` (line 187) is always True -- it's a dead check. The `too_far` filter on line 192 is the actual protection. Not a correctness bug since the filter catches it, but the code is misleading.

### 2.2 Spearman IC

Correct. Line 213: `sp_stats.spearmanr(signal[valid], returns[valid])` uses matching arrays with NaN/zero filtering. The `signal != 0.0` filter (line 209) excludes warmup zeros, which is appropriate.

### 2.3 Look-Ahead Bias Assessment

**Main IC decay curve**: No look-ahead bias. Signal at time t uses only data up to time t (EMA of past deltas). Forward returns use future data. Clean separation.

**Incremental IC**: MODERATE concern. Lines 361-367: `correction = alpha_coef * mlofi` where `alpha_coef` comes from `daily_regression()` fit on the SAME day's data. The regression coefficient is in-sample, which inflates the incremental IC. This does not affect the main IC results but means the incremental IC of +0.12 at 30s for 2330 is an optimistic upper bound. The artifact acknowledges coefficient instability (CV=56%) but does not flag this in-sample bias explicitly.

### 2.4 Newey-West Computation

Correct. Lines 228-241: Standard Bartlett kernel with `max_lag = floor(n^(1/3))`. Autocovariance computed correctly. Variance estimator includes `2 * w * gamma[j]` for cross-terms.

### 2.5 BUG: Realized Spread Direction Inversion (MODERATE)

**Line 557**: Comment says `positive MLOFI = we sell (TWSE contrarian)` but the artifact (Section "TWSE Sign Finding", line 144) states MLOFI is **pro-cyclical** on TWSE: positive MLOFI predicts price UP, alpha is positive on 29/31 days.

**Line 562**: `realized_spread = -direction * (mid_future - mid_at_fill)` assumes contrarian positioning (sell when MLOFI > 0). If MLOFI is pro-cyclical, we should BUY when MLOFI > 0, meaning `realized_spread = direction * (mid_future - mid_at_fill)`.

The sign inversion explains why realized spread is reported as negative even with IC = +0.206. With the corrected sign, realized spread would be positive (capturing the directional prediction), though it remains unclear whether it would overcome bid-ask spread costs.

**Impact**: The "realized spread is negative" conclusion in the artifact (Section "Realized Spread Per Fill") may be wrong for 2330. The reported numbers need recomputation with the corrected sign. This does not affect the IC results or kill gate conclusions, but undermines the monetization assessment.

### 2.6 NaN Handling

Adequate. `np.isfinite()` checks are used consistently. Days with fewer than 100 rows are skipped (line 71). IC computation requires minimum 30 valid observations (line 211). Regression requires minimum 100 (line 262).

---

## 3. Feature Index Mapping (Stage 1 Drift #1)

### Status: RESOLVED

Stage 1 issue: The proposal referenced `mlofi_gradient_x1000` which does not exist. The correct feature is `deep_depth_momentum_x1000` at index 20.

**Verification**: `src/hft_platform/feature/registry.py` line 151 confirms:
```
- deep_depth_momentum_x1000 [20]: Multi-Level Depth Momentum (L2-L5), scaled x1000.
```

The Stage 2 artifact does NOT propose a new feature index 21. The impl.py `feature_set_version="lob_shared_v2"` correctly references the existing feature set. The alpha is implemented as a standalone research alpha (not a FeatureEngine feature), so no new index is needed at this stage.

**Resolved**: No feature index drift in Stage 2.

---

## 4. Data Pipeline Assessment

### 4.1 L5 Data Loading

Data is loaded from `research/data/l5_v2/{symbol}_l5.npy` as numpy structured arrays with fields: `timestamp_ns`, `bids_price` (N,5), `bids_vol` (N,5), `asks_price` (N,5), `asks_vol` (N,5). Memory-mapped via `mmap_mode="r"` for efficiency. Day splitting uses regular trading hours (08:45-13:45 TW time).

### 4.2 TXFD6 L5 Coverage Issue

The 55% mean L5 coverage is a **structural limitation**, not a data quality problem. TXFD6 is a single front-month futures contract with thin depth at L3-L5. Many ticks have zero depth beyond L2, especially during quieter periods (Mar 6: 5.6%, Mar 11: 0.7%, Mar 13: 0%). This is inherent to the instrument.

This directly explains why MLOFI has zero predictive power on TXFD6: levels 2-5 carry no information when they are empty 45% of the time. The signal degenerates to L1-only OFI (already captured by existing features).

### 4.3 Production Deployment Concern

For any future production deployment on 2330, L5 data is available 100% of the time (TSMC is Taiwan's most liquid stock). However, the platform currently subscribes only to L1 data for equities via Shioaji. L5 subscription would require `quote_type="bidask5"` and changes to `ShioajiClient` / `FubonClient`. This is an infra gap.

---

## 5. Integration Path Assessment

### 5.1 Stage 1 Drift #2: MM Strategies Don't Consume Microprice from FeatureEngine

### Status: UNRESOLVED (Config Drift)

The `OpportunisticMM` strategy (`src/hft_platform/strategies/opportunistic_mm.py`) has an `on_features()` method that caches feature tuples, but it only consumes indices 16-18 (`ofi_depth_norm_ppm`, `ret_autocov_5s_x1e6`, `tob_survival_ms`) for the reversal filter. There is no microprice consumption path.

The Stage 2 artifact concludes TXFD6 is TERMINATE, making this moot for TXFD6. But the artifact proposes 2330 as a conditional pass, and there is no strategy that would consume a 2330 microprice correction. The integration path remains undefined.

**Status**: UNRESOLVED. The artifact acknowledges "Integration path unclear" (line 189) but does not propose a concrete resolution or document it as a known gap for Stage 3.

### 5.2 Stage 1 Drift #3: Cross-Symbol Propagation Not Supported

### Status: UNRESOLVED (Config Drift)

The platform has no cross-symbol signal propagation mechanism (e.g., 2330 MLOFI -> TXFD6/TMFD6 quoting adjustment). `FeatureEngine` computes features per-symbol independently. `StrategyRunner` dispatches events per-symbol. No cross-symbol bus exists.

The Stage 2 artifact acknowledges this implicitly (recommendations mention "cross-asset signal pathway" as unclear) but does not formally resolve or defer it.

Given the TXFD6 TERMINATE result, the cross-symbol path (2330 MLOFI -> TXFD6 quoting) is not worth pursuing: MLOFI is zero on TXFD6, and cross-symbol propagation would require significant infrastructure with no demonstrated value.

**Status**: UNRESOLVED. Should be formally documented as DEFERRED or CANCELLED.

### 5.3 Production Path Viability

**TXFD6**: Dead. No path forward.

**2330 as standalone**: Not viable without:
1. L5 market data subscription (infra change in `ShioajiClient`)
2. An equity strategy framework (current strategies are futures-focused)
3. Different fee/commission model (equity has different cost structure)
4. Queue priority simulation (equities have different matching rules)

**2330 as CBS filter**: Potentially viable (similar to R17 2330-as-TXFD6-filter), but requires L5 data infra.

**2330 as FeatureEngine feature**: Possible if L5 data becomes available, but the alpha coefficient instability (CV=56%) needs resolution first.

---

## 6. Config Drift Summary

| # | Drift | Severity | Stage 1 Status | Stage 2 Status | Notes |
|---|-------|----------|-----------------|----------------|-------|
| 1 | `mlofi_gradient_x1000` doesn't exist; correct name is `deep_depth_momentum_x1000` at index 20 | CRITICAL | FOUND | **RESOLVED** | Stage 2 impl uses correct `lob_shared_v2` reference; no new feature index proposed |
| 2 | MM strategies don't consume microprice from FeatureEngine | MODERATE | FOUND | **UNRESOLVED** | OpMM has `on_features()` but no microprice consumption. Artifact acknowledges gap but doesn't formally resolve |
| 3 | Cross-symbol propagation (TXFD6->TMFD6) not supported | MODERATE | FOUND | **UNRESOLVED** | No cross-symbol bus exists. Should be formally CANCELLED given TXFD6 TERMINATE |

**Unresolved drifts: 2**

---

## 7. Code Bug Summary

| # | Bug | File | Lines | Severity | Impact |
|---|-----|------|-------|----------|--------|
| 1 | Hot-path heap allocation of `cur_bp`/`cur_ap` numpy arrays on every `update()` | `impl.py` | 147-150 | MODERATE | Violates Allocator Law; blocks FeatureEngine promotion. Research-only OK. |
| 2 | Realized spread direction sign inverted (assumes contrarian but MLOFI is pro-cyclical on TWSE) | `backtest_ic.py` | 557, 562 | MODERATE | Monetization assessment ("realized spread negative") may be wrong for 2330. IC results unaffected. |

---

## 8. Verdict

### REJECT

**Reason**: 2 unresolved config drifts from Stage 1 (per review mandate: config drift > 0 = REJECT).

### Required fixes before re-submission:

1. **Drift #2**: Formally document the microprice consumption gap as either:
   - DEFERRED to a future stage with specific prerequisites (L5 infra + equity strategy), or
   - CANCELLED (no viable integration path at this time)

2. **Drift #3**: Formally document cross-symbol propagation as CANCELLED given TXFD6 TERMINATE.

3. **Bug #1** (impl.py): Pre-allocate `_cur_bid_price` and `_cur_ask_price` in `__init__` and `__slots__`. Not blocking for research but required for code quality standards.

4. **Bug #2** (backtest_ic.py): Fix realized spread direction sign and recompute. The monetization conclusion may change, which could affect the CONDITIONAL PASS recommendation for 2330.

### Assessment of results (independent of drifts):

The research findings are sound. TXFD6 TERMINATE is well-supported (zero IC, unstable coefficients, negative incremental IC). 2330 CONDITIONAL PASS is reasonable given IC=+0.206 at 30s with NW t=39.97, though the monetization question needs re-examination after Bug #2 is fixed. The structural insight (MLOFI works on deep equity books but not thin futures) is valuable for future alpha development prioritization.
