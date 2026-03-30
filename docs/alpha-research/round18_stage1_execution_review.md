# R18 Stage 1 Execution Review: Tradability Assessment

**Date**: 2026-03-26
**Reviewer**: Execution Reviewer Agent
**Survey Under Review**: `docs/alpha-research/round18_stage1_literature_survey.md`
**Candidates**: TSM-CR (A), HMM-RCM (B), VRB (C) -- all TAKER strategies, 1min-4h holding periods
**Status**: Complete

---

## Cost Model Correction

The Researcher uses **RT cost = 1.33 bps** throughout the survey. The correct figures are:

| Item | Researcher | Corrected |
|------|-----------|-----------|
| RT cost (NTD) | 40 | **39.2** (tax 6.6 + comm 13 per side) |
| RT cost (pts) | 4.0 | **3.92** |
| RT cost (bps) | 1.33 | **1.19** |

**Impact**: The correction is small and **favors** all candidates (lower cost = easier breakeven). IC breakeven thresholds should be recalculated:

| Horizon | Researcher IC Breakeven (1.33 bps) | Corrected IC Breakeven (1.19 bps) |
|---------|-----------------------------------|-----------------------------------|
| 30 min | 0.043 | **0.038** |
| 1 hour | 0.030 | **0.027** |
| 4 hours | 0.043 (survey value) | **0.038** |

All candidates benefit slightly. Non-blocking.

---

## Candidate A: Trend-Scaled Momentum with Cubic Reversion (TSM-CR)

### Tradability Checklist

| # | Check | Assessment | Pass? |
|---|-------|-----------|-------|
| 1 | **Latency vs signal half-life** | Signal operates on 1-4 hour price returns. 36ms submit RTT is completely irrelevant at this timescale. Entry via market order -- latency tolerance is extremely high. Even 500ms RTT would be acceptable. | PASS |
| 2 | **Data pipeline feasibility** | Requires: rolling price returns over 1h, 4h windows. Computable from TMFD6 tick data (9.16M rows, 58 days) in ClickHouse. No exotic data needed -- pure price returns. For live: mid_price from LOBStatsEvent is sufficient. | PASS |
| 3 | **Feature engine compatibility** | Core signal (trend t-stat phi, cubic model coefficients) is NOT in FE v2 and SHOULD NOT be. This is a price-return strategy, not an LOB feature. Correct approach: compute phi internally in the strategy from mid_price_x2 [2] streamed via LOBStatsEvent. No FE v2 changes needed. ATR for exits can also be computed internally from price history. | PASS |
| 4 | **Session handling** | Survey proposes CBS-style ToD gating (exclude opening 15min, closing 5min). CBS reference implementation in `cascade_bounce.py` already has wall-clock session gating logic (lines 46-50). Can be reused directly. **Day vs night**: The 1-4h hold means a day-session trade (entered 10:00) exits by 14:00 -- this crosses into the 13:45 close. Must handle forced exit at session end. Night session (15:00-05:00, 14h) is long enough for 4h holds but needs separate trend regime validation per Risk Factor #4 in survey. | CONDITIONAL |
| 5 | **Position sizing** | Vol-targeted with Kelly fraction. 1 lot on TMFD6 (margin ~16,700 NTD). Strategy proposes 2-5 trades/session. If single-lot sequential (close before re-enter), consistent with max_position=1. If concurrent positions allowed, need config. Recommend: **single lot, sequential** for Stage 2. | PASS |
| 6 | **Risk integration** | Market orders -- standard RiskEngine flow (OrderIntent -> RiskDecision -> OrderCommand). StormGuard HALT blocks new entries (correct). ATR trailing stop is strategy-internal, not risk-engine managed. Daily loss limit (5,000 NTD) provides hard backstop. No special risk integration needed. | PASS |
| 7 | **CBS interaction** | CBS holds 300s (5 min) after 40 bps moves. TSM-CR holds 1-4 hours on trend signals. **Directional conflict possible**: CBS enters contrarian (fade the move), TSM-CR could enter WITH the trend if phi is in weak-trend regime. Simultaneous opposing positions on same symbol. However, CBS is already in mutex_group "TMFD6_directional". TSM-CR should join this group. Alternatively: TSM-CR can check CBS position state before entry. | CONDITIONAL |
| 8 | **Cost model** | Corrected: 3.92 pts / 1.19 bps. At 1-4h horizon, IC breakeven = 0.027-0.038. Survey estimates IC 0.03-0.08. The lower end (0.03) is right at breakeven. **Marginal but plausible.** Stage 2 must demonstrate IC > 0.04 to have meaningful edge after costs. | PASS |

### Implementation Blockers

1. **58-day data is TIGHT for cubic model calibration**. Survey acknowledges this. Rolling 20-day calibration window leaves ~38 OOS days. With 2-5 trades/day, that's 76-190 trades. Borderline for statistical significance (need N >= 60 at minimum). Not a blocker for Stage 2 prototyping but IS a blocker for promotion confidence.

2. **Session boundary handling for 4h holds**. A trade entered at 12:00 with 4h target exit at 16:00 crosses the day session close (13:45). Must force-exit at session end OR allow overnight hold (risky for Stage 2). Recommend: force-exit 5 min before session close for Stage 2.

3. **Cubic model coefficients (b, c) universality assumption**. Safari & Schmidhuber's results aggregate 24 assets over 14 years. TMFD6 is a single retail-dominated mini futures contract. The cubic reversion may not hold. Stage 2 must validate b > 0 and c < 0 on TMFD6 specifically.

### Verdict: **APPROVE**

Lowest implementation complexity of the three. Signal is pure price-return based, no FE changes needed, latency is a non-issue. The main risk is data sufficiency (58 days) which is inherent to all candidates. Recommend as **second priority** (after VRB) per Researcher's ranking.

---

## Candidate B: HMM Regime-Conditioned Momentum (HMM-RCM)

### Tradability Checklist

| # | Check | Assessment | Pass? |
|---|-------|-----------|-------|
| 1 | **Latency vs signal half-life** | Operates on 5-minute bars. Decisions made once per 5 min. 36ms RTT is irrelevant. Even with 30-second decision lag, the 5min-2h holding period absorbs it trivially. | PASS |
| 2 | **Data pipeline feasibility** | Requires: 5-minute OHLCV bars aggregated from ticks. Computable from TMFD6 tick data in ClickHouse. For live: aggregate from LOBStatsEvent mid_price + TickEvent volume. Side information: RV(5min)/RV(30min) ratio -- computable from 1-min returns. Volume profile -- computable from TickEvent. All inputs available. | PASS |
| 3 | **Feature engine compatibility** | HMM state inference is NOT an FE feature and should not be. It's a strategy-internal model. Inputs: 5-min bar returns (from mid_price_x2 [2]), RV ratio (from price returns), volume (from TickEvent). No FE v2 changes needed. **However**: Baum-Welch calibration is computationally expensive. Must run offline, not in hot path. Deploy frozen parameters intraday. This is an Async Law concern -- calibration MUST be offloaded to a separate thread/process. | CONDITIONAL |
| 4 | **Session handling** | Survey notes day/night may need separate models (Risk Factor #5). Day session: 300 min = 60 five-minute bars. Night session: 840 min = 168 bars. Separate HMM calibration per session is feasible but doubles the model count. For Stage 2, recommend **day session only**. | PASS |
| 5 | **Position sizing** | Kelly-fraction * vol-target. Single lot, max 2h hold. 3-8 trades/session is higher frequency than A or C. If trades are sequential, single lot is fine. If overlapping signals fire, need queuing logic. Recommend: **single lot, sequential, skip if already positioned**. | PASS |
| 6 | **Risk integration** | Same as Candidate A -- standard market order flow through RiskEngine. No special integration needed. | PASS |
| 7 | **CBS interaction** | HMM-RCM trades at 5min-2h horizon. When HMM says "trending" and CBS fires contrarian, they WILL conflict. CBS detects 40 bps move -> fade. HMM detects trending regime -> go with trend. **This is a direct contradiction.** Must resolve via: (a) mutex_group, or (b) priority rule (CBS wins during its 300s hold, HMM defers). | FLAG |
| 8 | **Cost model** | Corrected: 1.19 bps. At 30min horizon, IC breakeven = 0.038. Survey estimates IC 0.04-0.10. Lower end is close to breakeven. At 1h horizon, breakeven = 0.027 -- comfortably within range if HMM calibrates well. | PASS |

### Implementation Blockers

1. **Baum-Welch computational cost**. HMM recalibration on 30-day rolling window involves iterative EM algorithm. For a 2-state HMM with ~8640 observations (30 days * 288 five-min bars), this takes ~100-500ms on a single core. **MUST NOT run on the event loop** (Async Law). Must be offloaded to a background thread/process with results loaded atomically.

2. **Regime label instability**. HMM states can swap labels between calibration windows (state 1 becomes state 2). Survey acknowledges this (Risk Factor #2). Implementation must anchor states by emission mean sign (positive drift = trending, negative/zero = reverting). This is solvable but adds implementation complexity.

3. **Forward-looking bias risk**. Survey correctly flags that Viterbi (smoothed) probabilities introduce look-ahead. Must use strictly filtered (forward-pass only) probabilities. In backtesting, this is a common mistake. Stage 2 prototype MUST be verified for look-ahead contamination.

4. **CBS directional conflict**. When a 40 bps move triggers CBS contrarian entry AND the HMM identifies a trending regime, the strategies send opposing orders. This is not just a position conflict -- it's a conceptual contradiction. Resolution: HMM-RCM should respect CBS's mutex_group "TMFD6_directional" and defer during CBS's active hold period.

### Verdict: **CONDITIONAL APPROVE**

Conditions:
- (C1) Baum-Welch calibration MUST be offloaded from event loop (Async Law). Architecture must be specified before prototyping.
- (C2) Forward-looking bias must be explicitly guarded against in backtesting framework (filtered probabilities only, no Viterbi).
- (C3) CBS conflict resolution must be designed before live deployment (mutex_group inclusion or priority rule).
- (C4) Day session only for Stage 2. Night session model deferred.

---

## Candidate C: Volatility-Regime Breakout (VRB)

### Tradability Checklist

| # | Check | Assessment | Pass? |
|---|-------|-----------|-------|
| 1 | **Latency vs signal half-life** | 30min-4h holding period. 1-3 trades per session. Breakout detection operates on 5min RV windows. 36ms RTT is completely irrelevant. | PASS |
| 2 | **Data pipeline feasibility** | Requires: 1-minute and 5-minute returns for RV computation, 1h and 4h RV windows, 20-day rolling percentile for RV compression detection, 4h EMA slope for directional bias. ALL computable from TMFD6 mid_price in ClickHouse. For live: aggregate from LOBStatsEvent mid_price_x2. Simple arithmetic -- no exotic inputs. | PASS |
| 3 | **Feature engine compatibility** | RV, ATR, EMA slope are NOT FE v2 features and should be strategy-internal (price-return derived, not LOB-derived). No FE changes needed. All computed from mid_price_x2 [2] which is FE v2 [2]. | PASS |
| 4 | **Session handling** | Survey notes vol compression is more common in night session. Day session: 300 min -- enough for 1h and 4h RV windows (the 4h window at session start uses prior session data or warm-up from overnight). Night session: 840 min -- ample. For Stage 2, recommend **day session only** to simplify. The 4h RV window at 08:45 open will be partially filled from previous night close -- need warm-up logic or start trading after 4h from session open (12:45, leaving only 1h of day session). **This is a significant constraint for day-only trading.** | CONDITIONAL |
| 5 | **Position sizing** | 1 lot, 1-3 trades/session. Sequential (close before re-enter). Simplest position management of all three candidates. Consistent with max_position=1. | PASS |
| 6 | **Risk integration** | Market orders through standard RiskEngine. ATR trailing stop is strategy-internal. 2x ATR stop on TMFD6 with ~0.1%/min sigma: ATR(1h) ~ 50-100 pts, stop = 100-200 pts = 1000-2000 NTD per lot. Well within daily loss limit (5,000 NTD) even with 3 consecutive stop-outs. | PASS |
| 7 | **CBS interaction** | Survey explicitly notes VRB and CBS are **complementary**: VRB catches initial breakout, CBS catches the reversion after overshoot. **However**, they can hold simultaneous positions. VRB enters WITH the breakout direction, CBS enters AGAINST after 40 bps move. If the breakout exceeds 40 bps, CBS fires while VRB is still holding -- opposing positions on same symbol. Possible scenarios: (a) VRB long + CBS short = net flat (wastes 2x RT cost), (b) VRB exits on ATR stop, CBS holds -- acceptable. **Need position-aware logic**: if VRB is long and CBS fires short, should CBS close the VRB position instead of opening a new short? Or should VRB check CBS state? | FLAG |
| 8 | **Cost model** | Corrected: 1.19 bps. At 1h horizon, IC breakeven = 0.027. Survey estimates IC 0.05-0.12 conditional on vol compression trigger. Well above breakeven. The selective entry (only during vol compression -> expansion) naturally filters low-quality signals. Cost model is favorable. | PASS |

### Implementation Blockers

1. **4h RV warm-up at session start**. The 4h RV window requires 4 hours of price data. At day session open (08:45), there's no prior 4h of day data. Options: (a) use overnight session data to warm up (introduces cross-session contamination), (b) start trading only after 12:45 (leaves just 1h of day session -- unacceptable), (c) use a shorter initial RV window (1h) for early session with graceful upgrade to 4h. Recommend option (c) for Stage 2.

2. **RV percentile calibration**. 20-day rolling percentile for vol compression detection. With 58 days of data, the first 20 days are calibration-only, leaving 38 OOS days. Same data sufficiency concern as Candidate A, but less severe since the RV percentile is simpler than cubic model fitting.

3. **VRB + CBS simultaneous position risk**. Both strategies trading TMFD6 can hold opposing positions simultaneously. The "complementary" framing in the survey is conceptually valid but operationally creates a net-flat-at-double-cost scenario. Must resolve before live deployment. Options: (a) add VRB to mutex_group "TMFD6_directional", (b) implement position-aware entry logic where CBS checks for existing VRB position and vice versa.

### Verdict: **APPROVE**

Simplest candidate, lowest overfitting risk, naturally selective entry, well-suited for TMFD6's volatility seasonality. Warm-up constraint (blocker #1) is solvable. CBS interaction (blocker #3) needs design but not a Stage 2 blocker (can run VRB-only in backtest). Recommend as **first priority** per Researcher's ranking.

---

## Cross-Cutting Issues

### 1. All Candidates Are Price-Return Strategies -- No FE v2 Changes Needed

None of the three candidates require new FeatureEngine v2 features. All operate on price returns (mid_price_x2 [2]) and derived statistics (RV, EMA, t-stat) computed internally. This is a deliberate and correct design: separating LOB microstructure features (FE) from price-return strategies (strategy-internal). FE v2's 21 features remain unchanged.

### 2. CBS Position Mutex

All 3 candidates create TMFD6 positions that could conflict with CBS_TMFD6:

| Scenario | CBS Direction | Candidate Direction | Conflict? |
|----------|-------------|-------------------|-----------|
| 40 bps drop + weak trend | SHORT (contrarian) | A: LONG (with weak downtrend -- no, contrarian too) | Possible alignment |
| 40 bps drop + strong trend | SHORT (contrarian) | A: SHORT (fade strong trend -- contrarian too) | Aligned |
| Trending regime + 40 bps move | SHORT (contrarian) | B: LONG (with trend) | **CONFLICT** |
| Vol breakout + 40 bps overshoot | CBS shorts after overshoot | C: LONG (with breakout) | **CONFLICT** |

**Recommendation**: Add all new strategies to mutex_group "TMFD6_directional". For Stage 2 backtesting, run each candidate independently (CBS off) to isolate signal quality. Address mutex in Stage 3 before shadow deployment.

### 3. Session Boundary Forced Exit

All 3 candidates have holding periods that can span session boundaries:
- A: 1-4h hold, day session = 5h -> can overflow
- B: 5min-2h hold -> fits within session
- C: 30min-4h hold -> can overflow

**Mandatory for all**: Implement forced exit 5 minutes before session close (13:40 for day, 04:55 for night). CBS already has this pattern (`_DEFAULT_SESSION_END_SEC = 13:35`). Reuse the same mechanism.

### 4. Config Drift Check

| Parameter | Survey Value | Platform Value | Drift? |
|-----------|-------------|---------------|--------|
| RT cost | 1.33 bps | **1.19 bps** (39.2 NTD) | **YES** (minor, favors candidates) |
| Submit RTT P95 | 36ms | 36ms | No |
| Modify RTT P95 | 43ms | 43ms | No |
| Cancel RTT P95 | 47ms | 47ms | No |
| TMFD6 data | 9.16M rows, 58 days | Confirmed | No |
| FE v2 features | "18 features" in survey | **21 features** [0-20] (registry.py) | **YES** (survey undercounts, non-blocking since candidates don't use FE) |
| TMFD6 session day | 08:45-13:45 | Confirmed | No |
| TMFD6 session night | 15:00-05:00 | Confirmed | No |

Config drift = 2 items (cost model minor, FE count mismatch irrelevant). **Non-blocking**.

### 5. Computational Cost on Hot Path

| Candidate | Hot-Path Compute | Async Law Risk |
|-----------|-----------------|----------------|
| A (TSM-CR) | Rolling EMA + cubic evaluation | Negligible (<1us) -- PASS |
| B (HMM-RCM) | Forward-pass probability update per 5min bar | ~10-50us per bar -- PASS. But Baum-Welch recalibration (~100-500ms) MUST be offloaded |
| C (VRB) | RV computation from 1-min returns | Negligible (<1us) -- PASS |

Only Candidate B has an Async Law concern (Baum-Welch recalibration). Must be offloaded to background thread.

---

## Overall Recommendation

### Priority Order (Aligned with Researcher)

1. **Candidate C (VRB)** -- APPROVE. Simplest, lowest overfitting risk, complementary to CBS, selective entry. 4h RV warm-up solvable. First to prototype.

2. **Candidate A (TSM-CR)** -- APPROVE. Novel cubic reversion model, pure price-return, low implementation complexity. 58-day data marginality is the main concern. Second to prototype.

3. **Candidate B (HMM-RCM)** -- CONDITIONAL APPROVE. Highest theoretical ceiling but highest implementation complexity (HMM calibration, label stability, Async Law offloading, forward-looking bias risk). Third priority, stretch goal.

### Mandatory Stage 2 Deliverables

1. **For all**: Compute trade-level statistics from 58-day TMFD6 backtest: N trades, win rate, avg P&L per trade (in pts), Sharpe, max DD. Use corrected cost of 3.92 pts RT.
2. **For A**: Validate b > 0 (trend persistence) and c < 0 (cubic reversion) on TMFD6 specifically. If b <= 0, kill A.
3. **For B**: Demonstrate filtered (not smoothed) HMM probabilities produce positive edge. Any use of Viterbi = instant rejection.
4. **For C**: Measure vol compression frequency and subsequent breakout direction accuracy on TMFD6. If breakout direction (4h EMA sign) accuracy < 55%, kill C.
5. **For all**: Run with CBS OFF to isolate candidate signal quality. CBS interaction design deferred to Stage 3.

### Stage 2 Kill Gates

| Metric | Threshold | Kills |
|--------|-----------|-------|
| N trades (58-day backtest) | < 40 | Candidate (insufficient sample) |
| Net P&L per trade | < 0 pts after 3.92 pts RT cost | Candidate |
| Max drawdown | > 2,000 NTD (40% of daily limit) | Candidate |
| Win rate | < 40% | Candidate (unless avg win >> avg loss) |
| TSM-CR: b coefficient sign | b <= 0 on TMFD6 | A specifically |
| HMM-RCM: look-ahead contamination | Any Viterbi usage | B specifically |
| VRB: breakout direction accuracy | < 55% | C specifically |

---

*Execution Reviewer -- R18 Stage 1*
