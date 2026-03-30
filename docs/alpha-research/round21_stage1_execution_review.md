# Round 21 Stage 1: Execution Review

**Reviewer**: Execution Reviewer (Claude Opus 4.6)
**Date**: 2026-03-27
**Survey**: `docs/alpha-research/round21_stage1_survey.md`
**Status**: CONDITIONAL APPROVE (Candidate B only) / REJECT (Candidates A, C)

---

## 1. Feature Index Mapping

**Verdict: PASS**

The survey references:
- `ret_autocov_5s_x1e6` at index 17
- `tob_survival_ms` at index 18

**Evidence**: Verified against two independent sources:

1. `src/hft_platform/feature/registry.py` lines 148-149:
   ```
   - ret_autocov_5s_x1e6 [17]: Albers et al. 2502.18625
   - tob_survival_ms [18]: Albers et al. 2502.18625
   ```

2. `src/hft_platform/feature/engine.py` lines 544, 568:
   ```
   # [17] ret_autocov_5s_x1e6: lag-1 autocovariance of mid_price_x2 returns
   # [18] tob_survival_ms: ms since last best price change
   ```

3. `src/hft_platform/strategies/opportunistic_mm.py` lines 38-39 confirm the same indices:
   ```python
   _IDX_RET_AUTOCOV_5S_X1E6 = 17
   _IDX_TOB_SURVIVAL_MS = 18
   ```

Feature tuple construction (`engine.py` line 524): `v1_tuple + v2_base + (iss_val, mldm_val)` where `v2_base` is `(ofi_depth_norm_ppm[16], ret_autocov_5s_x1e6[17], tob_survival_ms[18])`, then ISS at [19] and MLDM at [20]. All indices match.

---

## 2. Signal Flow (Cross-Strategy Communication)

**Verdict: FAIL -- CONFIG DRIFT detected**

The survey states (Candidate A, line 148):
> "Signal flow: `VpinRegimeSwitchStrategy` emits signal (+1/0/-1) -> OpMM reads signal via strategy coordinator -> OpMM adjusts internal gamma"

And for Candidate B (line 224):
> "VPIN signal: Subscribe to `VpinRegimeSwitchStrategy` signal output (already exists)"

**Problem**: There is NO "strategy coordinator" or cross-strategy communication mechanism in the codebase.

Evidence:
- `grep` for `coordinator`, `cross_strat`, `strategy_signal`, `get_signal` across the entire `src/hft_platform/` directory returns zero matches (except VPIN's own docstring).
- `StrategyRunner.process_event()` (`runner.py` lines 496-610) iterates over all registered strategies and dispatches events independently. Each strategy receives the same events through `strategy.handle_event(ctx, event)`. No strategy can read another strategy's internal state.
- The `StrategyContext` object passed to each strategy contains only `positions` -- not references to other strategies.
- `VpinRegimeSwitchStrategy` is not even registered in `config/base/strategies.yaml`. It has no config entry at all.
- The VPIN strategy exposes its signal via a `signal` property (line 766), but no other code reads this property.

**Config drift items**:
1. `VpinRegimeSwitchStrategy` is not in `strategies.yaml` -- it must be added and enabled.
2. No cross-strategy signal bus exists -- OpMM cannot read VPIN's `.signal` property.
3. The survey assumes infrastructure ("strategy coordinator") that does not exist.

**Required fix before prototyping**: Either:
- (a) Build a lightweight cross-strategy signal bus (e.g., a shared dict on `StrategyContext` where strategies publish signals by ID), OR
- (b) Compute VPIN regime directly inside OpMM (duplicate the logic, simpler but violates DRY), OR
- (c) Compute VPIN offline as a FeatureEngine feature (cleanest approach -- make VPIN a FE v3 feature at a new index), OR
- (d) Use the FeatureEngine `ret_autocov_5s_x1e6` as a proxy for regime (already available, no new infrastructure needed).

Option (d) is the pragmatic path for Candidate B: the survey already uses `ret_autocov_5s_x1e6` for the reversal filter. For VPIN conditioning, the value-add over autocovariance is questionable (R12/R19 evidence: VPIN is essentially volume intensity).

---

## 3. Latency Compatibility

**Verdict: PASS (Candidate B) / FAIL (Candidate A)**

### Pipeline latency budget:
From `config/research/latency_profiles.yaml`:
- Internal decision pipeline: 250 us (0.25 ms)
- Shioaji P95 submit RTT: 36 ms
- Total event-to-order: ~36.25 ms

### Candidate B (Dynamic Threshold):
The survey correctly notes (Section 6.2): "We can react to spread widening within ~1-2 ticks [125ms cadence]. This is fast enough for regime-level decisions."

The dynamic threshold is evaluated on each `LOBStatsEvent` in `on_stats()`. When spread widens, the gate opens on the SAME event. The 36ms RTT is the time from gate-pass to order-at-exchange. This is acceptable because:
- Wide-spread episodes (Section 4, Diagnostic D2) need median duration > 200ms to be capturable
- At 125ms tick cadence and 36ms RTT, we react in 1 tick + 36ms = ~161ms
- The survey correctly identifies this as a prerequisite diagnostic (D2)

**PASS** -- latency-compatible for reactive threshold gating.

### Candidate A (AS Framework Gamma Scaling):
The survey itself acknowledges (Section 6.2): "Full AS-framework quoting (continuously adjust quotes) is NOT latency-compatible at 36ms." The Appendix calculation confirms the gamma-dependent spread adjustment is sub-tick (<< 1 pt) on TMFD6.

**FAIL** -- the survey's own analysis rejects this candidate on latency grounds.

---

## 4. VPIN Warmup and Calibration

**Verdict: FAIL -- warmup period too long for opening session**

From `vpin_regime_switch.py`:
- `warmup_bars` = 60 (default)
- `bar_volume_target` = 500 (default)
- Total volume needed for warmup: 60 x 500 = 30,000 contracts

TMFD6 daily volume is typically ~50,000-100,000 contracts across a ~4.75 hour session (08:45-13:30).
- Average volume rate: ~10,500-21,000 contracts/hour = 175-350 contracts/minute
- Time to accumulate 30K volume: **86-171 minutes (1.4 - 2.9 hours)**

This means VPIN calibration completes between 10:15 and 11:45 TST. The morning opening session (08:45-10:00), which the survey itself identifies as requiring special handling (ToD adjustment +1 pt), would be ENTIRELY uncovered by VPIN conditioning.

Additionally, `VPINCalculator.is_warm` requires `_count >= _n_buckets` (50 buckets). Combined with the 60-bar warmup, the effective warmup requires both 60 bars AND 50 filled VPIN buckets. Since each bar fills one bucket, the binding constraint is 60 bars.

**Impact on candidates**:
- Candidate A: Critical -- gamma scaling depends entirely on VPIN regime
- Candidate B: Moderate -- ToD and volatility adjustments work without VPIN; VPIN conditioning is one of three factors
- Candidate C: Moderate -- same as B

**Required fix**: If VPIN conditioning is used, either:
1. Reduce `bar_volume_target` to 100-200 (warmup in ~30 min), with validation that VPIN quality degrades acceptably, OR
2. Use previous-day calibrated thresholds as startup defaults (persistent calibration), OR
3. Drop VPIN conditioning entirely for the opening 90 minutes, rely on ToD + volatility only

---

## 5. Points vs BPS

**Verdict: PASS**

All spread-related calculations in the survey use points consistently:
- `base_threshold = 5` (pts, line 167)
- `threshold_min = 4` (pts, line 219)
- `threshold_max = 10` (pts, line 220)
- VPIN adjustments: +2 pts (TOXIC), -1 pt (LOW) -- all integer point adjustments
- ToD adjustments: +1 pt -- integer points
- Volatility adjustments: +1/-1 pt -- integer points
- `compute_dynamic_threshold` returns integer points, clamped to [4, 10]
- Final comparison: `spread_pts < dynamic_threshold` -- both in points

The existing `OpportunisticMM.on_stats()` (line 187) computes `spread_pts = event.spread_scaled // _PRICE_SCALE` (integer division, points). The survey's integration plan (line 223) modifies this same method.

No BPS contamination detected. Consistent with feedback memory `feedback_spread_threshold_points.md`.

---

## 6. Risk Configuration -- 4 pt Threshold During LOW Regime

**Verdict: FAIL -- negative expected value after adverse selection**

The survey proposes lowering threshold to 4 pts during VPIN LOW regime (Candidate B, line 195: `threshold -= 1`).

The survey's own Section 6.3 contradicts this:
> "Edge needed = RT cost (4 pts) + adverse selection (1-2 pts) = 5-6 pts minimum spread for profitable trading."
> "This SUPPORTS the current threshold of 5 pts and suggests we should NOT lower it to 4 during LOW regime."

At 4 pts spread:
- RT cost: 4 pts (breakeven)
- Edge per trade: 0 pts (before adverse selection)
- After adverse selection (1-2 pts): **-1 to -2 pts per trade**
- At 10 NTD/pt: **-10 to -20 NTD per trade expected loss**

The survey acknowledges this in Section 6.3 but the pseudocode still includes `threshold -= 1` for LOW regime. This is an internal contradiction.

**Required fix**: Set `threshold_min = 5` (not 4). The "breakeven floor" at 4 pts is a misleading label -- it is breakeven before adverse selection, which means net negative. The true floor must be at 5 pts to maintain the 1-pt edge that compensates for back-of-queue adverse selection.

Alternatively, the diagnostic D1 could empirically measure whether LOW-regime trades at 4-pt spread are profitable (adverse selection may be lower during LOW regime). But until proven, the default must be 5 pts.

---

## 7. Additional Findings

### 7.1 VPIN Strategy Not in Config
`VpinRegimeSwitchStrategy` has no entry in `config/base/strategies.yaml`. Before any VPIN-dependent feature can work, this strategy must be:
1. Added to `strategies.yaml` with appropriate params
2. Enabled alongside `OPPORTUNISTIC_MM_TMFD6`
3. Tested to confirm it doesn't interfere with OpMM event processing

### 7.2 Cross-Strategy Signal Propagation Latency
Even if a signal bus is built, there is a timing issue: `VpinRegimeSwitchStrategy` updates its `signal` property during `on_tick()` or `on_stats()`. If `StrategyRunner` dispatches to strategies sequentially (which it does -- line 523 iterates `executors_iter`), the order of dispatch determines whether OpMM sees the current or previous VPIN signal. This introduces a 1-event propagation delay. At 125ms tick cadence, this is negligible for regime-level decisions but must be documented.

### 7.3 Candidate A Appendix Confirms Sub-Tick Adjustment
The survey's Appendix (lines 470-474) calculates that for TMFD6 parameters:
```
gamma * sigma^2 * tau / 2 ~ 7.5e-8
```
This is 4 orders of magnitude below the tick size (1 pt = 0.0001 in scaled terms). The AS framework gamma adjustment literally cannot produce a visible effect on TMFD6. This alone kills Candidate A.

### 7.4 Candidate C Aggression Adjustment
Candidate C proposes `aggression = +1/-1` to adjust quote offset. On a discrete LOB with 1-pt tick size, this means quoting 1 tick tighter or wider. At breakeven spread of 4 pts and threshold of 5 pts:
- Spread = 5, aggression +1: quote at best bid/ask (0 offset) -- this is joining the queue, adverse selection maximized
- Spread = 5, aggression -1: quote at best bid -1 / ask +1 (2 pt from mid) -- unlikely to fill

The discrete LOB makes aggression scaling a binary choice (join queue vs. don't) rather than a smooth parameter. This undermines the theoretical elegance of the hybrid approach.

---

## Summary

| Check | Candidate A | Candidate B | Candidate C |
|-------|------------|------------|------------|
| Feature index mapping | PASS | PASS | PASS |
| Signal flow (cross-strategy) | FAIL | FAIL | FAIL |
| Latency compatibility | FAIL | PASS | PASS |
| VPIN warmup timing | FAIL (critical) | FAIL (moderate) | FAIL (moderate) |
| Points vs BPS | PASS | PASS | PASS |
| Risk config (4 pt floor) | N/A | FAIL | FAIL |
| Sub-tick gamma effect | FAIL (kills A) | N/A | N/A |

---

## Overall Verdict

### Candidate A: **REJECT**
Three independent kill signals: (1) sub-tick gamma adjustment on TMFD6 makes it mathematically ineffective, (2) latency-incompatible continuous quoting at 36ms RTT, (3) VPIN warmup covers less than half the trading session. The survey itself reaches this conclusion in Sections 6.2-6.4 and the Appendix. Defer indefinitely.

### Candidate B: **CONDITIONAL APPROVE**
The dynamic threshold approach is sound and addresses a real problem (TOXIC-regime trades at spread=5 may be losers). However, the following must be fixed before prototyping:

**Required fixes (blocking)**:
1. **Raise threshold floor from 4 to 5 pts** -- the survey's own adverse selection analysis (Section 6.3) proves 4 pts is expected-loss. Remove the `vpin_low_subtractor` adjustment or set it to 0.
2. **Resolve cross-strategy signal gap** -- either (a) embed VPIN computation inside OpMM, (b) promote VPIN to a FeatureEngine feature, or (c) drop VPIN conditioning for the initial prototype and rely on ToD + volatility only. Option (c) is recommended for Stage 2 diagnostic.
3. **Add `VpinRegimeSwitchStrategy` to `strategies.yaml`** if VPIN conditioning is kept.
4. **VPIN warmup plan** -- document which conditioning factors are active during warmup period. At minimum, ToD and volatility gates must function independently of VPIN.

**Non-blocking recommendations**:
- Run Diagnostic D2 (wide-spread duration) FIRST -- if median duration < 200ms, abort all candidates.
- Run Diagnostic D1 without VPIN conditioning first (ToD + volatility only) as a simpler baseline.

### Candidate C: **REJECT (premature)**
Depends on Candidate B showing positive results. The aggression parameter on a discrete 1-pt-tick LOB is effectively binary (join queue vs. don't), undermining the smooth scaling premise. Revisit only if B demonstrates measurable VPIN conditioning value.

---

## Config Drift Register

| Item | Survey Assumption | Actual State | Severity |
|------|------------------|--------------|----------|
| Strategy coordinator | Exists | Does not exist | HIGH |
| VpinRegimeSwitchStrategy in config | Registered and enabled | Not in strategies.yaml | HIGH |
| Threshold floor | 4 pts (breakeven) | Should be 5 pts (after adverse selection) | MEDIUM |
| VPIN warmup coverage | Covers full session | Covers ~50-70% of session | MEDIUM |

---

## Infrastructure Gaps

1. **Cross-strategy signal bus**: No mechanism for Strategy A to read Strategy B's internal state. This is a platform-level gap that would benefit multiple future use cases (any regime/filter strategy feeding into an execution strategy).
2. **Persistent VPIN calibration**: No mechanism to save/load calibrated VPIN thresholds across sessions. Each session restarts cold.
3. **FeatureEngine VPIN feature**: If VPIN is valuable as a regime indicator, it should be a first-class FE feature (index [21]?) rather than computed inside a separate strategy. This would solve the signal propagation problem cleanly.
