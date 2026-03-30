# Stage 1 Execution Review: CBS Expansion Directions (Round 21)

**Date**: 2026-03-27
**Reviewer**: Execution Reviewer (Alpha Research Team)
**Artifact**: `round21_stage1_cbs_expansion_survey.md`
**Status**: REVIEW COMPLETE

---

## Review Summary

| Direction | Verdict | Key Issue |
|-----------|---------|-----------|
| **D1: RC-CBS** | CONDITIONAL APPROVE | EWMA(RV) not in FeatureEngine -- must be strategy-local. Walk-forward is offline-only (OK). |
| **D2: VAET** | CONDITIONAL APPROVE | ATR not in FeatureEngine -- must be strategy-local. Max position duration needs hard cap. Trailing stop widening needs max-loss bound. |
| **D3: OQS** | APPROVE | All inputs derivable from existing CBS detection window data. O(1) per trigger. No new features needed. |

**Overall**: No REJECTs. All directions are executable with caveats noted below. No config drift detected -- all proposed changes are additive parameters on top of existing CBS interface.

---

## 1. Latency Profile vs Signal Half-Life

**Reference latency** (from `config/research/latency_profiles.yaml`):
- Shioaji sim P95 RTT: **36 ms** (submit), 43 ms (modify), 47 ms (cancel)
- Local decision pipeline: **250 us**
- TMFD6 median tick interval: **125 ms**

**Minimum executable signal half-life**: 2x RTT = 72 ms

### Per-direction assessment:

**D1 (RC-CBS)**: PASS. Regime classification uses EWMA(RV) with halflife=30min. Regime state changes on timescale of minutes to hours. Signal half-life >> RTT by 3-4 orders of magnitude. No sub-RTT reaction required.

**D2 (VAET)**: PASS. Exit decisions (trailing stop, momentum exhaustion) operate on tick-by-tick updates but the hold period is 120s-600s. The trailing stop check is evaluated per-tick but the DECISION timescale is seconds, not sub-RTT. ATR computation window is ~5min+. No latency concern.

**D3 (OQS)**: PASS. Quality score is computed ONCE at trigger time (when CBS detects the 40 bps move). This is a gate/filter, not a reactive signal. Computation happens before entry order placement. The ~250us pipeline latency for the score computation is negligible vs the 600s detection window.

---

## 2. Feature Index Mapping

**FeatureEngine v2** (`lob_shared_v2`) provides 21 features at indices [0]-[20]:

| Index | Feature | Relevant to D1/D2/D3? |
|-------|---------|----------------------|
| [0]-[5] | best_bid, best_ask, mid_price_x2, spread_scaled, bid_depth, ask_depth | D2 (spread gate), D3 (mid for move speed) |
| [6] | depth_imbalance_ppm | No |
| [7] | microprice_x2 | No |
| [8]-[10] | l1_bid_qty, l1_ask_qty, l1_imbalance_ppm | No |
| [11]-[13] | ofi_l1_raw, ofi_l1_cum, ofi_l1_ema8 | No |
| [14]-[15] | spread_ema8_scaled, depth_imbalance_ema8_ppm | D2 (spread regime) |
| [16] | ofi_depth_norm_ppm | No |
| [17] | ret_autocov_5s_x1e6 | Potentially D3 (reversal predictor) |
| [18] | tob_survival_ms | No |
| [19] | impact_surprise_x1000 | No |
| [20] | deep_depth_momentum_x1000 | No |

### Missing features:

**D1 needs EWMA(RV)**: NOT in FeatureEngine. Realized volatility (sum of squared mid_price_x2 returns over a window) is not computed by the engine. However, this is trivially computed strategy-local: maintain a ring buffer of mid_price_x2 deltas (same pattern as `_PriceEntry` deque in current CBS), compute sum-of-squares over window. Cost: O(1) amortized per tick with a ring buffer. **No FeatureEngine change required.** Implementation: ~20 LOC inside strategy.

**D2 needs ATR**: NOT in FeatureEngine. ATR (Average True Range) requires high/low/close per period. TMFD6 L1 data provides bid/ask but not explicit high/low bars. Approximation: use max(mid_x2) - min(mid_x2) over N-tick windows as a proxy for true range. This is already what Vol-CBS does in `research/alphas/vol_cbs/impl.py`. Cost: O(1) with min/max ring buffer. **No FeatureEngine change required.** Implementation: ~30 LOC inside strategy.

**D3 needs move_speed, acceleration**: Fully derivable from the existing CBS detection window deque (`_price_buf`). move_speed = move_bps / duration_s (already computed implicitly). acceleration = peak_speed - avg_speed requires scanning the deque at trigger time. max_retracement requires tracking max adverse excursion during build-up. Cost: O(N) where N = deque length at trigger time (max 8192 entries, but typically ~4800 entries for 600s at 125ms). This runs ONCE per trigger (~5-15 triggers/day), NOT per tick. **Acceptable.**

**D3 vol_ratio**: Needs current ATR or RV at trigger time. Same as D1/D2 -- strategy-local computation.

### Implementation cost summary:

| Feature | LOC | Hot-path impact | FeatureEngine change? |
|---------|-----|----------------|----------------------|
| EWMA(RV) | ~20 | O(1)/tick, pre-allocated ring buffer | No |
| ATR proxy | ~30 | O(1)/tick, min/max ring buffer | No |
| move_speed | ~5 | O(1) at trigger only | No |
| acceleration | ~15 | O(N) at trigger only (~5-15x/day) | No |
| max_retracement | ~10 | O(N) at trigger only | No |

---

## 3. Cost Model Accuracy

**Researcher's cost assumptions** (from survey lines 27, 67):
- RT cost: ~4 pts (1.19 bps) -- **CORRECT**
- Breakdown: tax 6.6 NTD/side + commission 13 NTD/side = 39.2 NTD total
- Point value: 10 NTD/pt -> 39.2 / 10 = 3.92 pts, rounded to ~4 pts
- March median spread: 3 pts < 4 pts cost

**Verification**: All cost numbers match `feedback_mini_taiex_point_value.md` (1 pt = 10 NTD) and `feedback_taifex_fee_structure.md` (full retail costs, no maker rebates). PASS.

**Does any direction address the structural spread < cost problem?**

- **D1 (RC-CBS)**: Partially. In low-vol regime (March tight spread), D1 proposes k=4.0 (higher threshold) and hold=400s (longer hold). Higher threshold means fewer triggers but larger expected reversion. Longer hold gives more time for reversion to overcome costs. However, if median spread = 3 pts and cost = 4 pts, the ENTRY itself crosses the spread adversely. D1 does NOT explicitly address entry cost.
- **D2 (VAET)**: Yes, partially. Limit exit from R20 (+17 pts improvement) is incorporated. Limit orders save ~1 pt on exit (avoiding crossing the spread). This directly reduces effective RT cost by ~1 pt.
- **D3 (OQS)**: Indirectly. By filtering low-quality triggers (which are more likely to stop out), OQS reduces the fraction of trades that pay RT cost without capturing reversion. Does not reduce per-trade cost but improves win rate.

**Risk**: None of D1/D2/D3 fundamentally solves the March tight-spread problem. If median spread remains 3 pts with 4 pts RT cost, CBS needs >4 pts average reversion capture to be profitable. Current OOS is +3 bps = ~10 pts on TMFD6 (mid ~33,000 pts), so the edge exists in favorable regimes. The question is whether D1 can correctly AVOID trading in unfavorable regimes.

---

## 4. Config vs Research Params Consistency

**Current CBS implementation** (`cascade_bounce.py`):

| Parameter | Current Default | Type | Interface |
|-----------|----------------|------|-----------|
| `move_threshold_bps` | 40 | `int` | Constructor kwarg |
| `detect_window_ns` | 600_000_000_000 | `int` | Constructor kwarg |
| `hold_ns` | 300_000_000_000 | `int` | Constructor kwarg |
| `stop_loss_bps` | 15 | `int` | Constructor kwarg |
| `cooldown_ns` | 5_000_000_000 | `int` | Constructor kwarg |
| `session_start_sec` | 33300 (09:15) | `int` | Constructor kwarg |
| `session_end_sec` | 48900 (13:35) | `int` | Constructor kwarg |

**D1 proposed changes**:
- Per-regime parameter lookup: `{high_vol: (k=2.5, stop=2.0*ATR, hold=200s), low_vol: (k=4.0, stop=1.0*ATR, hold=400s)}`
- Compatible: These map directly to existing constructor params. D1 would add a regime classifier + param switcher, but the underlying CBS logic uses the SAME parameters. No `BaseStrategy` or `StrategyRunner` changes needed.
- New params needed: `regime_thresholds`, `regime_params_high`, `regime_params_low`. Additive -- no breaking change.

**D2 proposed changes**:
- Replace fixed stop with ATR-trailing stop
- Replace fixed hold with conditional hold (capped [120s, 600s])
- Add momentum exhaustion exit
- Compatible: `_check_exit()` is fully internal to `CascadeBounceStrategy`. No interface change to `BaseStrategy`. The trailing stop replaces the current `pnl_bps < -self._stop_loss_bps` check. Hold period becomes dynamic but stays within the existing `elapsed_ns >= self._hold_ns` pattern (just with a dynamic `_hold_ns`).
- New params: `trailing_stop_atr_mult`, `hold_min_ns`, `hold_max_ns`, `momentum_exit_threshold`. Additive.

**D3 proposed changes**:
- Quality score gate before entry
- Compatible: Inserted into `_check_entry()` after move detection, before order placement. No interface change.
- New params: `quality_threshold`, `quality_weights` (or individual weight params). Additive.

**Config drift check**: **ZERO config drift.** All changes are additive parameters. Existing CBS with default params would behave identically to current implementation. No `BaseStrategy` or `StrategyRunner` modifications required.

---

## 5. Risk Limits

### Current CBS risk profile:
- Max position duration: 300s (fixed hold)
- Max loss per trade: 15 bps (fixed stop)
- Single position at a time (enforced by `_state` dict)

### D1 (RC-CBS) risk assessment:
- Hold period: 200s (high-vol) to 400s (low-vol). **Max position duration: 400s.** Acceptable (33% increase from 300s).
- Stop: 2.0*ATR (high-vol). In high-vol regime (Jan-Feb), ATR could be ~15-20 pts -> stop at 30-40 pts -> ~9-12 bps. In extreme vol, ATR could spike -> stop widens. **RISK: No hard cap on ATR-scaled stop.** MUST add `max_stop_bps` parameter (e.g., 30 bps) as a circuit breaker.
- Recommendation: Add hard cap `stop_bps = min(k * ATR_bps, max_stop_bps)`.

### D2 (VAET) risk assessment:
- Hold period: [120s, 600s] capped. **Max position duration: 600s.** Double current default but explicitly capped. Acceptable.
- Trailing stop: `entry +/- max(s * ATR, RT_cost)`. Floor at RT_cost prevents degenerate tight stops. **RISK: In high-vol, trailing stop widens proportionally.** Same concern as D1 -- needs hard cap.
- Momentum exhaustion exit: exits EARLY if reversion captures >50% of move. This REDUCES risk (shorter exposure). PASS.
- Limit exit: passive limit order. Risk: partial fill or no fill within hold window. Must have fallback to market exit at hold expiry. Current CBS already does this (exits at best bid/ask at hold expiry).
- **Worst case**: 600s hold with trailing stop at 2x ATR in high-vol. If ATR = 20 pts, trailing stop = 40 pts = ~12 bps. With hard cap at 30 bps, max single-trade loss = 30 bps * 300,000 NTD = 900 NTD. Acceptable for single-contract TMFD6.

### D3 (OQS) risk assessment:
- OQS is a FILTER (reduces entries). It can only REDUCE risk relative to base CBS. No new risk exposure. PASS.

### Mandatory risk bounds (for D1 + D2):
1. `max_stop_bps: int = 30` -- hard cap on any ATR-scaled stop
2. `max_hold_ns: int = 600_000_000_000` -- hard cap on dynamic hold (already proposed in D2)
3. Single-position constraint must be preserved (already enforced in current implementation)

---

## 6. Implementation Feasibility

### Allocator Law compliance:

**D1**: EWMA(RV) uses a pre-allocated ring buffer of `int` values. Regime classification is a comparison against 2 thresholds. Parameter lookup is a dict access. **No heap allocations on hot path.** PASS.

**D2**: ATR proxy uses pre-allocated min/max ring buffer. Trailing stop update is arithmetic on existing state variables. Momentum exhaustion check is arithmetic on entry price + current price. Limit order placement uses existing `self.buy()`/`self.sell()` path. **No heap allocations on hot path.** PASS.

**D3**: Quality score computation at trigger time scans the existing `_price_buf` deque. This deque already exists and is maintained per-tick. The scan is O(N) but runs only at trigger time (~5-15x/day). Logistic regression weights are fixed floats loaded at init. **No new heap allocations.** PASS.

### Walk-forward calibration:

**D1**: Walk-forward (5-day IS -> 1-day OOS) is **offline-only** (research backtest). The strategy receives regime boundaries as config params. No real-time adaptation needed during live trading. In production, regime boundaries would be updated daily (pre-market) based on prior N-day data. This is a config update, not a hot-path operation. PASS.

**D2**: Hold period scaling uses `target_vol / current_vol` ratio. `current_vol` is computed strategy-local per-tick (EWMA). `target_vol` is a config param set offline. **No real-time calibration needed.** PASS.

**D3**: Quality score weights calibrated via IS logistic regression. Weights are static during trading. Re-estimated offline every 5 days. **No real-time calibration needed.** PASS.

---

## 7. Additional Observations

### 7.1 ret_autocov_5s_x1e6 [17] for D3
The FeatureEngine already computes `ret_autocov_5s_x1e6` which measures lag-1 autocovariance of mid_price_x2 returns. Negative autocov = oscillating prices = reversal likely. This is a potentially valuable input to the OQS quality score. The Researcher did not mention it, but it is available at zero cost via `ctx.get_feature(symbol, "ret_autocov_5s_x1e6")`. **Suggestion**: include ret_autocov as a candidate OQS feature.

### 7.2 Deque maxlen concern
Current CBS `_price_buf` has `maxlen=8192`. At 125ms tick cadence, this covers 8192 * 0.125s = 1024s (~17 min). The detection window is 600s. Sufficient headroom. D3's O(N) scan over ~4800 entries at trigger time takes <1ms in Python. Acceptable.

### 7.3 D1 + D2 interaction
D1 sets regime-conditional hold period, D2 overrides hold with volatility-adaptive hold. If both are implemented, which takes precedence? **Must define priority**: D1 sets the BASE hold per regime, D2 applies scaling ON TOP of the regime-conditional base. This interaction must be explicitly designed in Stage 2.

---

## Verdict

| Direction | Verdict | Conditions |
|-----------|---------|------------|
| **D1: RC-CBS** | **CONDITIONAL APPROVE** | (1) EWMA(RV) computed strategy-local, not added to FeatureEngine. (2) ATR-scaled stop MUST have hard cap `max_stop_bps=30`. (3) Walk-forward is offline-only config generation. |
| **D2: VAET** | **CONDITIONAL APPROVE** | (1) ATR proxy computed strategy-local. (2) Hard cap on trailing stop (`max_stop_bps=30`) and hold (`max_hold_ns=600s`). (3) Limit exit must have market-order fallback at hold expiry. (4) Define D1+D2 hold priority interaction explicitly in Stage 2. |
| **D3: OQS** | **APPROVE** | All inputs available. O(1) at trigger time (O(N) scan acceptable at ~5-15 triggers/day). Consider adding `ret_autocov_5s_x1e6` as candidate feature. |

**No config drift. No BaseStrategy/StrategyRunner changes needed. No FeatureEngine changes needed. All directions are compatible with existing CBS interface and Allocator Law.**

Priority ordering (D1 first, then D2+D3 parallel) is sound from an execution perspective.

---

## Config Drift Register

| Item | Survey Assumption | Actual State | Severity |
|------|------------------|--------------|----------|
| (none) | -- | -- | -- |

**Zero config drift detected.** All three directions propose additive changes compatible with existing CBS implementation.
