# Round 18 Stage 2 Challenger Review: MLOFI Microprice Correction

**Date**: 2026-03-27
**Reviewer**: Challenger Agent
**Artifact**: `docs/alpha-research/round18_stage2_mlofi_microprice.md`
**Code**: `research/alphas/mlofi_microprice/{impl.py, backtest_ic.py}`

---

## Challenge 1: TXFD6 TERMINATE May Be Premature Due to L5 Coverage Bias

**Claim challenged**: "MLOFI has ZERO predictive power on TXFD6" and the recommendation to TERMINATE.

**Why potentially wrong**: The Researcher acknowledges TXFD6 L5 coverage averages only 55%, with several days catastrophically low (Mar 6: 5.6%, Mar 11: 0.7%, Mar 13: 0%). When L5 data is missing, the MLOFI signal degenerates to near-zero (missing levels contribute zero OFI). The IC is then computed across the full tick series -- including the ~45% of ticks where the signal is structurally uninformative. This dilutes any genuine IC present in the well-covered subset.

The BBO-shift guard further compounds this: when prices at levels 2-5 change (which happens frequently when levels appear/disappear due to sparse coverage), OFI is zeroed out. The effective signal-active fraction of the data may be well below 55%.

**Data needed**:
1. Recompute IC on TXFD6 conditioned on L5 coverage > 80% per-tick. If only 3-4 days have decent L5 coverage, compute per-day IC restricted to high-coverage ticks within those days.
2. Report what fraction of ticks have non-zero MLOFI after all guards (BBO shift + level shift) fire. If <20% of ticks carry signal, the IC computation is dominated by noise.
3. Separate the 14 days into a "good L5" cohort (>70% coverage) and "bad L5" cohort (<40% coverage). Is there a statistically meaningful IC difference between cohorts?

**Verdict**: **OPEN**. The TERMINATE decision may be based on signal computed over mostly-garbage input data. This does not prove MLOFI works on TXFD6 -- but it means we cannot conclusively say it does not work either. The null result is confounded by data quality.

---

## Challenge 2: 2330 IC Monotonically Increasing With Horizon Is Suspicious

**Claim challenged**: IC = +0.093 at 250ms rising to +0.212 at 60s on 2330, interpreted as "strong predictive power."

**Why potentially wrong**: Genuine microstructure signals exhibit IC decay -- they predict immediate price moves, and predictability fades as noise accumulates. An IC that INCREASES monotonically from 250ms to 60s is a red flag for one of three confounds:

**(a) Overlapping return autocorrelation inflation.** At 30s horizon with median tick interval ~50ms on 2330, the forward return windows overlap massively (each 30s window shares ~99.8% of its ticks with the next). Spearman IC computed on overlapping returns inflates the apparent daily IC and suppresses cross-day variance, producing artificially high NW t-statistics. The reported NW t-stat of 39.97 at 30s (with only 17 days) is implausibly large -- this exceeds what published microstructure studies achieve with years of data.

**(b) Daily trend contamination.** MLOFI with EMA-8 smoothing is a slow-moving signal (EMA half-life ~6 ticks). On a trending day, MLOFI will be persistently positive (or negative) for extended periods. Forward returns at 30-60s on a trending day are also persistently directional. The IC then measures correlation between two slow-moving series, which captures the daily trend rather than tick-level microstructure prediction. This would explain why IC rises with horizon: longer horizons capture more trend, and the "signal" is really "today's direction."

**(c) Look-ahead contamination in `compute_forward_returns`.** Reviewing line 183-188 of `backtest_ic.py`: `searchsorted(..., side="left")` finds the first index >= target timestamp. When `target_ts == ts_ns[j]` exactly (possible with discrete timestamps), `future_idx[i] = j` and `fwd[i] = mid[j] - mid[i]`. This is correct (no look-ahead). However, after clipping (`np.clip(future_idx, 0, n-1)`), the check `valid = future_idx < n` on line 187 is always True (clipping already ensured this). So every tick gets a forward return, including the last ticks of the day where `future_idx` was clamped to `n-1`. These end-of-day ticks have `fwd = mid[n-1] - mid[i]`, which is a backward-looking measure contaminated by the full day's return. The `too_far` guard (line 192) should catch most of these, but only if `|actual_ts - target_ts| > h_ns` -- at exactly the 2x tolerance boundary, marginal contamination persists.

**Data needed**:
1. Compute IC using NON-OVERLAPPING returns: subsample every 30s (or 60s), then compute Spearman IC on those ~600 independent observations per day. Report the cross-day mean and t-stat. Expect IC to drop significantly.
2. Run a daily-trend-removal test: subtract the day's mean return from all forward returns, then recompute IC. If IC collapses, the signal is a trend proxy.
3. Verify end-of-day handling: print the number of ticks in the last 60s of each day that receive non-NaN forward returns at the 30s and 60s horizons.

**Verdict**: **OPEN**. The monotonically increasing IC profile is the single most concerning finding. Until non-overlapping IC and trend-decontaminated IC are provided, the 2330 result cannot be trusted.

---

## Challenge 3: Sign Reversal From Round 11 Lacks Rigorous Decomposition

**Claim challenged**: "Including L1 in the OFI integration flips the sign because L1 dominates."

**Why potentially wrong**: Round 11 found MLDM (L2-L5 depth momentum, no L1) with IC = -0.105 (contrarian). The current MLOFI uses L1-L5 with geometric weights `w_k = lambda^(k-1)`, meaning L1 gets weight 1.0 (since `lambda^0 = 1`) and L5 gets weight `0.5^4 = 0.0625`. Verified in `impl.py` line 113: `[lam ** k for k in range(N_LEVELS)]` gives `[1.0, 0.5, 0.25, 0.125, 0.0625]`.

So L1 has 16x the weight of L5. The "MLOFI" is effectively an L1 OFI signal with a tiny L2-L5 garnish. The sign flip is trivially explained: L1 OFI is well-known to be pro-cyclical (positive OFI = buy pressure = price up), and L1 dominates the composite. This is NOT the same as saying "MLOFI (multi-level) adds value" -- it means the Researcher is mostly measuring L1 OFI and attributing the result to multi-level depth.

The incremental IC analysis partially addresses this (IC of correction term vs L1 residual), but the incremental IC uses `alpha_coef * mlofi` as the correction, where `alpha_coef` is fitted IN-SAMPLE on the same day's data. This is circular: the regression coefficient is optimized to maximize in-sample R-squared, and then the fitted values are correlated with the same returns. The incremental IC should use an out-of-sample alpha (e.g., previous day's coefficient).

**Data needed**:
1. Decompose MLOFI into L1-only component and L2-L5-only component. Report IC of each separately. If L1-only IC >> L2-L5-only IC, the multi-level claim is weakened.
2. Recompute incremental IC using rolling out-of-sample alpha coefficients (e.g., train on day t-1, evaluate on day t). In-sample incremental IC is inflated.
3. Verify the Round 11 MLDM signal construction to confirm it genuinely excludes L1 (different from current MLOFI).

**Verdict**: **OPEN**. The sign flip is likely an artifact of L1 dominance rather than a genuine multi-level discovery. The incremental IC analysis suffers from in-sample bias.

---

## Challenge 4: Realized Spread Metric Is Misconceived

**Claim challenged**: "Even with strong IC, realized spread is negative -- the signal cannot overcome bid-ask costs."

**Why potentially wrong**: The realized spread computation in `backtest_ic.py` lines 529-569 is conceptually flawed in two ways:

**(a) Fill trigger is wrong.** Fills are triggered when `|MLOFI| > 0.5`. This assumes a market-making strategy where the signal determines quoting. But MLOFI is measured as a correction to microprice, not a fill trigger. In an actual OpMM or LP strategy, fills occur when counterparty lifts/hits our quotes. The fill probability depends on queue position, spread width, and market order flow -- none of which are modeled. The "realized spread" metric answers a question nobody asked.

**(b) Direction assignment is inverted relative to the sign finding.** Line 557: `direction = np.sign(mlofi[fill_mask])` with comment "positive MLOFI = we sell (TWSE contrarian)". But the Researcher's own Stage 2 finding is that alpha is POSITIVE (pro-cyclical): positive MLOFI predicts price UP. If positive MLOFI means price will go up, the maker should BUY (place bid), not sell. The realized spread formula on line 562: `realized_spread = -direction * (mid_future - mid_at_fill)` then computes the wrong sign for the pro-cyclical interpretation. This means the negative realized spread may partly be an artifact of the direction being inverted.

**Data needed**:
1. Correct the direction assignment to match the pro-cyclical finding (positive MLOFI = buy signal, negative MLOFI = sell signal), then recompute realized spread.
2. Better yet, abandon the realized spread metric entirely for this signal type. MLOFI microprice correction is a feature/filter, not a standalone fill generator. Evaluate it as a CBS filter or FeatureEngine input instead.

**Verdict**: **OPEN**. The realized spread metric is both conceptually misapplied and has a direction bug. It should not be used as evidence for or against the signal.

---

## Challenge 5: Forward Return Computation Has a Subtle Valid-Mask Bug

**Claim challenged**: Code correctness of `compute_forward_returns`.

**Why wrong**: In `backtest_ic.py` lines 183-188:

```python
future_idx = np.searchsorted(ts_ns, target_ts, side="left")
future_idx = np.clip(future_idx, 0, n - 1)
valid = future_idx < n  # <-- ALWAYS TRUE after clip
fwd[valid] = mid[future_idx[valid]] - mid[valid]
```

After `np.clip(future_idx, 0, n - 1)`, every element of `future_idx` is in `[0, n-1]`, so `future_idx < n` is always True. The `valid` mask does nothing. This means every tick in the array gets a forward return, including ticks near the end of the day where `searchsorted` would have returned `n` (beyond data), but clip forced it to `n-1`.

The `too_far` guard on line 192 mitigates this for most cases (the clamped ticks will have `|actual_ts - target_ts| > h_ns` and get NaN-ed). But this relies on the 2x tolerance being strict enough. For the 250ms horizon, a tick at `t = end - 200ms` has `target_ts = end + 50ms`, gets clamped to `n-1` (timestamp = `end`), and `|end - (end+50ms)| = 50ms < 250ms`, so it passes the tolerance check and gets a (wrong) forward return of `mid[end] - mid[t-200ms]` -- a backward-looking 200ms return, not a forward 250ms return.

This bug is small in magnitude (affects ~0.1% of ticks at day boundaries) but indicates insufficient testing of edge cases.

**Data needed**:
1. Fix the valid mask: `valid = np.searchsorted(ts_ns, target_ts, side="left") < n` BEFORE clipping.
2. Rerun IC to confirm results are materially unchanged (expected: negligible impact, but establishes code correctness).

**Verdict**: **OPEN** (minor). Bug confirmed in code. Impact likely negligible but must be fixed for correctness.

---

## Summary

| # | Challenge | Severity | Verdict |
|---|-----------|----------|---------|
| 1 | TXFD6 TERMINATE premature due to L5 coverage dilution | MEDIUM | OPEN |
| 2 | 2330 IC monotonically increasing -- overlapping returns + trend contamination | **HIGH** | OPEN |
| 3 | Sign reversal is L1 dominance artifact; incremental IC uses in-sample alpha | MEDIUM | OPEN |
| 4 | Realized spread direction bug + conceptual misapplication | MEDIUM | OPEN |
| 5 | Forward return valid-mask bug (always True after clip) | LOW | OPEN |

## Overall: REJECT

**5 challenges, all OPEN.**

The most critical issue is Challenge 2: the 2330 IC profile (monotonically increasing, NW t > 39 with 17 days) is inconsistent with known microstructure signal behavior and strongly suggests overlapping-return inflation and/or daily trend contamination. Until non-overlapping IC and trend-decontaminated IC are computed, the CONDITIONAL PASS for 2330 cannot be upheld.

The TXFD6 TERMINATE (Challenge 1) may also be premature, though this is lower priority -- even if MLOFI works on the high-coverage TXFD6 subset, the sparse L5 data makes it impractical for production.

### Conditions for Re-Approval

1. **[MUST]** Compute 2330 IC using non-overlapping (subsampled) returns at 30s. Report cross-day mean and proper t-stat.
2. **[MUST]** Run daily-trend decontamination on 2330 IC (subtract day mean return). Report residual IC.
3. **[SHOULD]** Decompose MLOFI into L1-only and L2-L5-only IC to validate the "multi-level value-add" claim.
4. **[SHOULD]** Fix the `compute_forward_returns` valid-mask bug and the realized spread direction assumption.
5. **[MAY]** Condition TXFD6 IC on L5 coverage > 80% to rule out data-quality confound.
