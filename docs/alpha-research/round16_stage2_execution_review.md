# Execution Review -- Round 16 Stage 2 (Prototype Validation)

**Reviewer**: Execution Reviewer
**Date**: 2026-03-26
**Subject**: Fill Probability Filter (Candidate C) + Inventory Skew Optimization (Candidate A)
**Codebase refs**: `research/alphas/fill_prob_filter/`, `research/alphas/inventory_skew_opt/`, `src/hft_platform/feature/registry.py`, `src/hft_platform/strategies/opportunistic_mm.py`

---

## 1. Code Correctness Review

### 1.1 Candidate C: Fill Probability Filter (`fill_prob_filter/impl.py`)

**Model implementation**: CORRECT

- Logistic regression with gradient descent + L2 regularization is correctly implemented (lines 241-269).
- Numerically stable sigmoid (line 189-192): handles both positive and negative logit branches. PASS.
- AUC via Mann-Whitney U statistic (lines 293-315): correct O(n*m) implementation. No external dependency needed.
- Feature extraction (lines 113-152): sign-adjustment for side on `depth_imbalance_ppm` and `ofi_l1_ema8` is correct -- this ensures "adverse" imbalance is always positive regardless of fill side.
- Welford's online normalizer (lines 94-110): numerically stable running mean/variance. PASS.
- `FillProbabilityFilter.should_quote()` (lines 342-380): uses pre-allocated `_feature_buf` (Allocator Law compliant), no heap allocation on hot path. PASS.

**Issue found**: The `FillProbabilityFilter.should_quote()` method writes directly into `self._feature_buf` and then calls `model.should_enter()` which calls `normalizer.normalize()`. The normalize method creates a new array via `(features - self.mean) / std_safe` -- this allocates on every call. In a production hot-path context this would violate the Allocator Law. **Severity: LOW** -- this is research prototype code, not production. If promoted, the normalize step must be inlined with pre-allocated output buffer.

### 1.2 Candidate C: Backtest (`fill_prob_filter/backtest.py`)

**Fill simulation**: METHODOLOGICALLY FLAWED (partial)

- **Fill detection logic (lines 164-166)**: `buy_filled = next_bid < bid_px or next_ask < ask_px`. This is overly aggressive. A buy fill at bid requires an aggressive seller hitting our bid, but the condition `next_ask < ask_px` is checking the ask side movement, which may indicate spread compression without our bid being filled. This overestimates fill rate and may explain the 99.9% filter pass rate (too many fills are spurious).
- **No queue position modeling**: The simulation assumes all wide-spread quotes are immediately fillable at the next tick. There is no queue-back assumption (consistent with prior Round 13 findings that queue priority is THE bottleneck). This is acceptable for a comparative filter evaluation but overestimates absolute fill counts.
- **Post-fill return calculation (lines 186-188)**: `post_fill_ret_bps = side * (future_mid - fill_price) / fill_price * 10000.0`. Correct: measures directional return from maker's perspective. Uses mid-price at +5s horizon, which is standard.

**IS/OOS split**: PARTIALLY CORRECT

- Split is time-ordered (first 60% IS, last 40% OOS) -- no future leakage in sample ordering. PASS.
- However, fills are collected per-day then concatenated (lines 243-245), so the split boundary falls within the concatenated sequence. This preserves temporal ordering. PASS.
- **Caveat**: The 60/40 split of fills (not days) means the IS/OOS boundary may fall mid-day. This is acceptable for fill-level models but could introduce intra-day regime bias if the split happens during a session. **Severity: LOW** -- unlikely to materially affect conclusions given the weak signal.

**Feature computation**: MINOR ISSUE

- EMA alpha calculation (line 113): `ema_alpha = 1.0 - (7.0 / 8.0) = 0.125`. This is a standard EMA-8 decay. However, the FeatureEngine's EMA-8 uses a different update formula. The research backtest computes its own EMA from raw tick data rather than using the platform's FeatureEngine output. This creates a **parity gap** between research and live. For a filter that achieves only AUC=0.556, this parity gap is irrelevant.

### 1.3 Candidate A: Inventory Skew (`inventory_skew_opt/impl.py`)

**Riccati ODE solver**: CORRECT

- The backward RK4 integration (lines 144-164) is numerically sound with appropriate clamping to prevent divergence.
- Terminal condition A(T) = 0 (line 99) is correct per Avellaneda-Stoikov.
- Stationary solution A_stat = sqrt(gamma * sigma^2 / (8 * Sigma_H)) matches Gueant et al. (2013) closed form.
- Sigma_H = lambda_0 * exp(-kappa * delta_bar) * kappa with delta_bar = 1/kappa (line 122): this correctly reduces to Sigma_H = lambda_0 * exp(-1) * kappa.

**Calibration (`calibrate_from_txfd6`)**: HAS A CIRCULAR DEPENDENCY

- Lines 316-324: gamma is calibrated so that the Riccati skew at q=5 equals half_spread/2. This means A_target = half_spread/10, and gamma is back-solved from A_target. This is **circular**: gamma is chosen to make the Riccati solution match a predetermined skew level. The "70x coefficient gap" mentioned in the summary is therefore a consequence of comparing two different gamma assumptions, not a fundamental structural incompatibility.
- **However**, this circularity actually strengthens the rejection: the model has no independent calibration source for gamma. The gamma parameter drives the entire skew magnitude, and without market-data-derived gamma (e.g., from realized PnL variance aversion), the Riccati solution has a free parameter that can be tuned to produce any desired skew. This makes it no better than the current hand-tuned INVENTORY_SKEW_DIVISOR.

### 1.4 Candidate A: Comparison (`inventory_skew_opt/comparison.py`)

- Correctly loads data, calibrates, solves Riccati, and compares. No methodological issues.
- Sensitivity analysis (lines 149-167) sweeps gamma, which is the right parameter to probe.
- Adiabatic ratio check (line 173): 36ms / 125ms = 0.288, correctly flagged as valid (< 1). PASS.

---

## 2. Backtest Methodology Assessment

### 2.1 IS/OOS Split: PASS (with caveat)

- Time-ordered split: YES (fills are temporally sequential per day, days loaded in date order).
- No future leakage: Normalizer is fit on IS data only, then applied to OOS. PASS.
- **Caveat**: The normalizer's running statistics (Welford) are computed over the entire IS set before normalization (lines 225-228 fit normalizer, lines 233-234 then normalize). This is correct -- no OOS contamination.

### 2.2 Adverse Fill Label: PARTIALLY CORRECT

- Definition: post-fill return < -0.5 bps at +5s = adverse. This is reasonable for a maker fill.
- **Issue**: The label measures return from fill_price, but a maker's true cost basis includes the bid-ask spread capture. A fill at bid with subsequent mid moving down by 0.5 bps may still be profitable if the spread capture exceeds the adverse move. The label should arguably be: `post_fill_ret_bps < -(spread_capture_bps / 2 - cost_bps)`. This is a known simplification in the Albers framework and is acceptable for relative model comparison.

### 2.3 Fill Realism: WEAK

- No queue position modeling (acknowledged above).
- Fill detection is based on price movement at next tick, which approximates aggressive order flow hitting our quote. This is generous -- real fills require queue priority.
- The mean post-fill return of +1.095 bps (positive without filter) suggests the simulated fills are biased toward favorable fills. In reality, wide-spread fills have elevated adverse selection (the paper's premise). The simulation may be detecting spread-capture events rather than true maker fills.

---

## 3. Candidate A Rejection Verification

### 3.1 "Riccati optimal = linear" claim: MATHEMATICALLY CORRECT

The AS/Gueant value function ansatz V(t,q) = -A(t)*q^2 - C(t) produces optimal quote offsets that are linear in inventory q:

    delta_bid(q) = 1/kappa + A*(1 + 2q)
    delta_ask(q) = 1/kappa + A*(1 - 2q)
    => skew(q) = delta_bid - delta_ask = 4*A*q (linear in q)

This is identical in functional form to the current SimpleMarketMaker's `skew_x2 = -(pos * tick_size * 2) // INVENTORY_SKEW_DIVISOR` which is also linear in position. The claim is correct.

### 3.2 70x Coefficient Gap: CALIBRATION ISSUE, NOT STRUCTURAL

The gap arises because:
1. The current INVENTORY_SKEW_DIVISOR=5 was hand-tuned for practical trading.
2. The Riccati coefficient depends on gamma (risk aversion), which the implementation calibrates circularly (see Section 1.3 above).
3. With the circular calibration (gamma chosen to match skew@q=5 to half_spread/2), the comparison would produce matched coefficients by construction. The 70x gap indicates a different gamma assumption was used.

**Verdict**: The gap is a calibration artifact, not a structural incompatibility. However, this does not rescue the candidate -- the absence of an independent gamma calibration means the Riccati solution offers no advantage over parameter-sweeping INVENTORY_SKEW_DIVISOR directly.

### 3.3 Would INVENTORY_SKEW_DIVISOR Sweep Be More Productive?

**YES, marginally**. A simple grid search over INVENTORY_SKEW_DIVISOR in [2, 3, 5, 8, 10, 15, 20] with walk-forward validation on TXFD6 data would achieve the same result as Riccati optimization but with:
- No model risk (no calibration assumptions)
- Direct PnL optimization (instead of proxy via risk aversion)
- 1 hour of compute vs. multi-day research

However, prior Round 13 findings showed that inventory skew is a secondary concern -- queue priority and adverse selection dominate PnL at 36ms RTT. A DIVISOR sweep would produce marginal (<0.1 bps) improvements at best.

**Recommendation**: Not worth pursuing as standalone effort. If a broader parameter sweep is planned (e.g., spread_threshold_bps + DIVISOR + IMBALANCE_COEFF), include DIVISOR as one dimension.

---

## 4. Feature Availability Check

### 4.1 Fill Probability Filter -- 7 Features

| Feature | Source in Model | FeatureEngine Feature | Index | Available in v1? | Available in v2? |
|---------|----------------|----------------------|-------|-----------------|-----------------|
| spread_z | `spread_scaled` | `spread_scaled` | 3 | YES | YES |
| depth_imbalance_z | `depth_imbalance_ppm` | `depth_imbalance_ppm` | 6 | YES | YES |
| ofi_ema8_z | `ofi_l1_ema8` | `ofi_l1_ema8` | 13 | YES | YES |
| l1_qty_ratio | `l1_bid_qty`, `l1_ask_qty` | `l1_bid_qty`, `l1_ask_qty` | 8, 9 | YES | YES |
| spread_ema_ratio | `spread_ema8_scaled` | `spread_ema8_scaled` | 14 | YES | YES |
| depth_imb_ema_z | `depth_imb_ema8_ppm` | `depth_imbalance_ema8_ppm` | 15 | YES | YES |
| spread_x_imbalance | interaction term | computed from [3] x [6] | - | YES (derived) | YES (derived) |

**All 7 features are available in `lob_shared_v1`** (indices 0-15). No v2 features required.

**Note**: The Stage 1 execution review incorrectly stated that `lob_shared_v2` "does not exist in the codebase." In fact, `lob_shared_v2` IS registered in `default_feature_registry()` (registry.py line 180) with 19 features (v1's 16 + `ofi_depth_norm_ppm` [16], `ret_autocov_5s_x1e6` [17], `tob_survival_ms` [18]). The v2 features are the ones used by `OpportunisticMM`'s reversal filter (lines 31-33 of `opportunistic_mm.py`).

### 4.2 OpportunisticMM v2 Feature Indices -- CORRECT

The `opportunistic_mm.py` uses:
- `_IDX_OFI_DEPTH_NORM_PPM = 16` -- matches `ofi_depth_norm_ppm` at v2 index 16. PASS.
- `_IDX_RET_AUTOCOV_5S_X1E6 = 17` -- matches `ret_autocov_5s_x1e6` at v2 index 17. PASS.
- `_IDX_TOB_SURVIVAL_MS = 18` -- matches `tob_survival_ms` at v2 index 18. PASS.

---

## 5. Verdicts

### Candidate C (Fill Probability Filter): **APPROVE** (agree with MARGINAL result)

**Reasoning**:
- OOS AUC = 0.556 is barely above random (0.5). The model has minimal discriminative power.
- Filter pass rate = 99.9% confirms the model cannot meaningfully separate adverse from favorable fills.
- The baseline mean post-fill return is already positive (+1.095 bps), which means the filter is solving a problem that may not exist in the simulation (likely due to overestimated fill quality in the backtest).
- The fill simulation has methodological weaknesses (overly generous fill detection, no queue modeling) that bias results toward positive returns, masking true adverse selection.
- Code quality is good: `__slots__`, pre-allocated buffers, numerically stable sigmoid. If the signal were stronger, the implementation would be production-ready with minor fixes (normalize allocation).
- **Conclusion**: The candidate correctly identifies that LOB-state-only features have insufficient information to predict adverse fills at useful AUC levels. This is consistent with Albers et al.'s finding that fill probability prediction requires queue position data, which we do not have.

### Candidate A (Inventory Skew Optimization): **APPROVE** (agree with REJECTED result)

**Reasoning**:
- The claim that "Riccati optimal skew is linear in inventory" is mathematically correct. The AS/Gueant framework produces exactly the same functional form as our current implementation.
- The 70x coefficient gap is a calibration artifact due to circular gamma fitting, not a structural model incompatibility with tick-constrained markets.
- The Riccati solution has one free parameter (gamma) that can produce any desired skew magnitude. Without an independent calibration source, it offers no advantage over direct INVENTORY_SKEW_DIVISOR tuning.
- The Barzykin (2603.07752) OTC-specific mechanics (trade rejection, reputation scores) were correctly stripped. What survives is standard Avellaneda-Stoikov, which is well-understood.
- **Conclusion**: The rejection is well-founded. The theoretical framework confirms our current linear skew is already the optimal functional form. The only question is coefficient calibration, which is better addressed by direct parameter optimization.

---

## 6. Round Disposition: TERMINATE

**Rationale**:

1. **Candidate C** (Fill Probability Filter): AUC too weak. No path to improvement without queue position data (which we structurally cannot obtain from Shioaji L1/L2 feeds).

2. **Candidate A** (Inventory Skew): Riccati framework confirms current approach is correct. No alpha available from skew optimization alone.

3. **Candidate B** (Depth-Normalized OFI / Reversal Filter): Already integrated into `OpportunisticMM` v2 as the reversal filter (lines 86-122 of `opportunistic_mm.py`). No further research needed -- it is a production feature awaiting live validation.

4. **No pivot candidates remain**: The three Stage 1 candidates have all been resolved (one integrated, one confirmed, one rejected). The Round 14 exhaustive exploration already established that TXFD6 directional signal ceiling is approximately 0.001 bps. Further microstructure signal mining is subject to diminishing returns.

**Recommendation**: Close Round 16. Next research investment should target:
- **INVENTORY_SKEW_DIVISOR parameter sweep** as part of a broader OpportunisticMM hyperparameter optimization (low effort, 1 compute hour).
- **Live shadow validation** of OpportunisticMM v2 (reversal filter) to measure real-world adverse selection rates and fill quality, which will provide ground truth for any future fill probability modeling.
- **TXO options data** (33M rows discovered in Round 14) for volatility surface / hedging research, which opens a structurally different alpha channel.
