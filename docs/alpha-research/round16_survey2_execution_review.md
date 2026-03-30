# Round 16 Survey V2: Execution Review of 3 New Candidates

**Reviewer**: Execution Agent
**Date**: 2026-03-26
**Document reviewed**: `docs/alpha-research/round16_survey2_updated_constraints.md`

---

## Platform Specifications (Verified for TMFD6)

| Parameter | Value | Source |
|-----------|-------|--------|
| Instrument | TMFD6 (Micro TAIEX April 2026) | `config/symbols.yaml` line 246 |
| Point value | 10 NTD | `config/symbols.yaml` (point_value: 10) |
| Tick size | 1 pt | `config/symbols.yaml` (tick_size: 1) |
| Broker RTT P95 submit | 36 ms | `config/research/latency_profiles.yaml` |
| FeeCalculator mapping | TMFD6 -> XMT | `config/base/fees/futures.yaml` line 41 (CORRECT) |
| Commission | 13 NTD/contract | `config/base/fees/futures.yaml` (XMT) |
| Config tax_rate_bps | 2.0 | `config/base/fees/futures.yaml` (likely wrong for futures) |

### Cost Model Verification (CRITICAL)

Survey claims: XMT cost = 40 NTD RT = 4.0 pts = 1.33 bps.

**Verified cost breakdown**:

| Component | NTD | Source |
|-----------|-----|--------|
| Commission (13 x 2 sides) | 26 | Config |
| TAIFEX futures tax (sell only) | 6.6 | Regulatory rate 0.00002 x 330,000 NTD notional |
| **Total (regulatory)** | **32.6** | **= 3.26 pts = 0.99 bps** |

| Component | NTD | Source |
|-----------|-----|--------|
| Commission (13 x 2 sides) | 26 | Config |
| Tax at survey rate (~0.42 bps) | 14 | Survey implicit assumption |
| **Total (survey)** | **40** | **= 4.0 pts = 1.21 bps** |

| Component | NTD | Source |
|-----------|-----|--------|
| Commission (13 x 2 sides) | 26 | Config |
| Tax at config rate (2.0 bps) | 66 | Config (likely stock rate, wrong for futures) |
| **Total (config)** | **92** | **= 9.2 pts = 2.79 bps** |

**Assessment**: The survey's 40 NTD figure uses a tax rate (~0.42 bps) that is between the TAIFEX regulatory rate (0.2 bps) and the platform config (2.0 bps). The exact TAIFEX Micro futures tax rate needs authoritative verification. Using the regulatory rate gives 32.6 NTD RT (3.26 pts, 0.99 bps) -- more favorable than the survey assumes. Using the config rate gives 92 NTD RT (9.2 pts, 2.79 bps) -- potentially strategy-killing.

**The config rate of 2.0 bps is almost certainly wrong for futures** (same bug identified in MC-1 review for TX). The TMFD6 -> XMT mapping in FeeCalculator IS correct (unlike TXFD6), but the underlying XMT tax rate may be incorrect.

**Recommendation**: Accept the survey's 40 NTD as a conservative middle ground until the TAIFEX regulatory rate is authoritatively confirmed. If actual rate is lower (~33 NTD), the strategy has more headroom.

---

## Data Availability and Quality

### Verified Data Inventory

| Metric | Survey Claim | Verified | Status |
|--------|-------------|----------|--------|
| Total rows | 9.16M | 5.14M (in npy files) | **DRIFT**: ~4M rows may exist in ClickHouse only |
| Days | 58 | 14 unique dates in npy exports | **DRIFT**: 14 days exported, not 58 |
| L5 depth data | Available | **NOT FOUND** in `research/data/raw/tmfd6/` | **DRIFT**: No L2/L5 npy files exist |
| Tick rate | 1.8/sec | 4.5/sec (Jan), 11.3/sec (Mar) | **DRIFT**: Actual rate is 2-6x higher |

**Critical data gaps**:
1. **No L5 data exported**: The survey claims L5 depth data is available (likely in ClickHouse), but no L2/L5 npy files exist under `research/data/raw/tmfd6/`. Candidate B (CatBoost with L5 features) cannot proceed without L5 data export.
2. **Only 14 days exported** (not 58): The remaining data may be in ClickHouse but has not been exported. Walk-forward validation on 14 days is marginal.

### Jan/Feb vs March Spread Regime (ROOT CAUSE ANALYSIS)

**This is NOT the same far-month artifact as TXFD6.**

| Metric | TMFD6 Jan 28 | TMFD6 Mar 19 | TXFD6 Jan 28 |
|--------|-------------|-------------|-------------|
| Median spread | 28 pts | 3 pts | 232 pts |
| Tick rate | 4.5/sec | 11.3/sec | 3.8/sec |
| Median bid_qty | 5 | 4 | 2 |
| P10 spread | 12 | 2 | 60 |
| P90 spread | 76 | 4 | 3,121 |

TMFD6 Jan/Feb data is **actively traded** (4.5 ticks/sec, meaningful queue depth, spreads in a tradable range of 12-76 pts). This is fundamentally different from TXFD6 Jan which was degenerate far-month (232 pts median, 0% narrow spreads).

**The TMFD6 spread regime difference (28 vs 3 pts) appears to be a genuine market structure effect**, not a contract-month artifact. Possible causes:
- Micro TAIEX has wider spreads during certain market conditions
- Jan/Feb 2026 may have been a high-volatility period
- Contract liquidity builds gradually (Jan = 3 months before April expiry, March = 1 month before)

**Impact**: The survey's use of both Jan/Feb and March data is **valid**. The wide-spread regime IS the target for Candidate C (spread regime prediction), and the survey correctly identified that execution timing works best during wide spreads.

---

## Candidate A: Push-Response Anomalies (Conditional Mean Reversion)

### Signal Half-Life vs RTT: PASS

- Signal operates on lag ranges of 10-5000 ticks. At 4.5-11.3 ticks/sec, this is 1-1111 seconds.
- 36ms RTT is negligible relative to these timescales.
- No speed advantage needed -- this is a seconds-to-minutes mean reversion trade.

### Feature Compatibility: GOOD

Existing FeatureEngine v1 features usable:
- `mid_price_x2` [2] -- price tracking for push detection
- `spread_scaled` [3] -- spread regime context
- `ofi_l1_raw` [11] -- order flow during push

**NEW features needed**:
1. Push magnitude detector (rolling max drawdown over N ticks) -- ~40 LOC, single scalar state, <1us
2. Response tracker (price change since push) -- ~20 LOC, trivial

Minimal feature implementation cost. Most logic is strategy-level (detect push, post contrarian limit, exit on reversion).

### Infrastructure Feasibility: PASS

- Contrarian limit order after large push fits standard `StrategyRunner -> OrderIntent -> RiskEngine -> OrderAdapter` pipeline.
- Passive limit order avoids taker costs.
- Timeout/exit logic is standard strategy behavior.

### Cost-to-Signal Ratio: NEEDS VALIDATION

- Survey claims large pushes produce measurable conditional responses on SPY.
- At ~33-40 NTD RT cost (3.3-4.0 pts), need conditional response > 4 pts net of cost.
- On TMFD6, a "large push" during wide-spread regime (median 28 pts spread) could easily be > 20 pts. If mean reversion captures even 30% of a 20 pt push, that's 6 pts gross -- potentially profitable after 4 pts cost.
- In tight-spread regime (median 3 pts), pushes are much smaller and probably not actionable.

### Push-Response Feasibility on TMFD6: CONDITIONAL PASS

The SPY push-response paper uses 1,500 days of NBBO data (~390M+ events). We have 5.14M ticks across 14 days. The push-response map requires large sample sizes to detect conditional effects. **14 days may be insufficient for robust push-response estimation**, particularly for large pushes (rare events by definition).

However, at 4.5-11.3 ticks/sec and ~335K-517K ticks per day, we have reasonable event counts for push magnitudes up to ~3 sigma. Pushes > 3 sigma will have very few observations.

### Candidate A Verdict: CONDITIONAL APPROVE

**Conditions**:
1. Quick validation (push-response map on TMFD6) is mandatory before any implementation.
2. Must use data from both Jan/Feb (wide-spread regime, where large pushes occur) and March (tight-spread) to assess regime dependence.
3. Focus on wide-spread regime where push magnitudes are larger relative to costs.
4. Need minimum ~100 observations per push-size bucket for statistical significance.

---

## Candidate B: Multi-Feature LOB Prediction with CatBoost

### Data Sufficiency: FAIL

**L5 data does not exist in exported form.** No L2/L5 npy files in `research/data/raw/tmfd6/`. The survey claims "TMFD6 ClickHouse data includes 5-level bid/ask arrays" -- this may be true in the raw ClickHouse storage, but it has not been exported for research use.

Without L5 data, the CatBoost multi-feature approach collapses to L1-only features, which we already know produce IC=0.04 (too weak).

### CatBoost Overfitting Risk with 58 Days (Claimed) / 14 Days (Actual)

Even at the claimed 58 days:
- 20+ features with CatBoost requires strict walk-forward validation
- Train on 40 days, test on 18 days = acceptable but tight
- At 1-second frequency: ~40 * 5hrs * 3600 = 720K training samples -- adequate for CatBoost
- **But**: 14 actual exported days means train ~10 / test ~4 -- **extremely marginal**

### Implementation Complexity: HIGH

- Feature engineering pipeline for 20+ LOB features: ~500-800 LOC
- CatBoost training/inference infrastructure: ~300 LOC
- Walk-forward validation framework: ~200 LOC
- Total: ~1,000-1,300 LOC, plus dependency on CatBoost library

### Candidate B Verdict: REJECT (for now)

**Reasons**:
1. **L5 data not available** in research format -- blocks multi-level feature computation
2. **14 exported days** is insufficient for walk-forward validation with 20+ features
3. **Highest implementation cost** of the three candidates (~1,300 LOC + CatBoost dependency)
4. **Overfitting risk** is extreme with this feature-to-sample ratio on limited OOS data

**Resolution path**: If L5 data is exported from ClickHouse AND more days are exported (minimum 40 for train + 18 for test), reconsider. But this should be lowest priority.

---

## Candidate C: Spread Regime Prediction for Execution Optimization

### Signal Half-Life vs RTT: PASS (N/A)

- This is a regime-level prediction (hours to days). RTT is irrelevant.
- The strategy adjusts execution approach based on current regime, not predicting tick-level events.

### Feature Compatibility: EXCELLENT

All needed features exist in FeatureEngine v1:
- `spread_scaled` [3] -- current spread
- `spread_ema8_scaled` [14] -- smoothed spread trend

**No new features required.** Regime detection is a rolling average threshold on existing spread data.

### Infrastructure Feasibility: PASS

- Spread regime detection can be a strategy-level state variable (rolling 5-minute average spread > threshold).
- Execution mode switching (timing-aware vs passive-only) fits within `StrategyRunner`.
- No new runtime planes or components needed.

### Cost-to-Signal Ratio: FAVORABLE

- This is not an alpha generator -- it is an **execution optimizer** that selects when to apply execution timing.
- Value: 2.6-9.4 pts/trade improvement in wide-spread regime (survey claim). Even at the conservative end (2.6 pts), this exceeds the 1.2 pts passive limit order savings and meaningfully improves execution quality.
- Cost: zero (no additional trades generated; same trades, better execution).
- **This is additive to any alpha strategy.** It improves execution of existing strategies.

### Data Sufficiency: PASS

- Spread autocorrelation can be tested on any amount of data.
- 14 days across two distinct regimes (wide and tight) is ideal for testing regime persistence.
- Spread is directly observable -- no feature engineering needed.

### Spread Regime Interpretation: VALIDATED

As shown in the data analysis above, TMFD6 Jan/Feb (wide spread, median 28 pts) vs March (tight spread, median 3 pts) is a **genuine market structure effect**, not a data artifact. The survey's use of both regimes is valid.

The question "Is this predictable or a known calendar effect?" is important but not blocking:
- Even if the regime is calendar-driven (predictable from contract age/rollover proximity), USING that information is still valuable.
- If intraday spread variation is autocorrelated (highly likely -- spread changes are persistent), the regime detector adds within-day value.

### Candidate C Verdict: APPROVE

**No conditions.** This is:
- Lowest risk (no directional prediction, no ML, no new features)
- Lowest implementation cost (~50 LOC for rolling spread threshold)
- Immediately testable (spread autocorrelation check)
- Additive to any future alpha strategy
- Validated by two distinct spread regimes in the data

Should be validated and implemented first regardless of what happens with A and B.

---

## Overall Assessment

| Candidate | Verdict | Confidence | Priority |
|-----------|---------|------------|----------|
| C: Spread Regime | **APPROVE** | High | 1st (immediate) |
| A: Push-Response | **CONDITIONAL APPROVE** | Medium | 2nd (quick validation) |
| B: CatBoost Multi-Feature | **REJECT** (data gaps) | High | 3rd (blocked on L5 export) |

### Config Drift Summary

| Item | Survey Claim | Verified | Drift |
|------|-------------|----------|-------|
| Total rows | 9.16M | 5.14M in npy | **DRIFT**: ~4M not exported |
| Days coverage | 58 | 14 exported | **DRIFT**: only 14 days in npy files |
| L5 data | Available | Not in npy files | **DRIFT**: no L2/L5 exports exist |
| XMT cost | 40 NTD RT | 33-40 NTD RT (regulatory-to-survey range) | Minor drift, favorable direction |
| Tick rate | 1.8/sec | 4.5/sec (Jan), 11.3/sec (Mar) | **DRIFT**: actual rate 2-6x higher |
| Median spread | 3 pts (Mar), 34 pts (Jan/Feb) | 3 pts (Mar), 28 pts (Jan) | Minor (28 vs 34) |
| Jan/Feb data usability | Used (wide-spread regime) | Valid (not far-month artifact) | OK |
| TMFD6 FeeCalculator mapping | N/A | TMFD6 -> XMT correctly mapped | OK |

### Recommended Validation Sequence (agreed with survey)

1. **Day 1, Check 1**: Spread autocorrelation at 5-minute scale (Candidate C). If autocorrelation > 0.8, implement immediately.
2. **Day 1, Check 2**: Push-response map on TMFD6 (Candidate A). Focus on pushes > 2 sigma in wide-spread regime.
3. **Day 2-3**: Prototype whichever passes validation.
4. **Deferred**: Export L5 data from ClickHouse for Candidate B. Only pursue after A and C are resolved.

### Platform Action Items

1. **Verify TAIFEX Micro futures tax rate** authoritatively (regulatory website or broker confirmation). Update `config/base/fees/futures.yaml` XMT `tax_rate_bps` if needed.
2. **Export remaining TMFD6 data from ClickHouse** (58 days claimed vs 14 exported). More data benefits all candidates.
3. **Export L5 depth data** for TMFD6 if Candidate B is to be reconsidered.

---

## Addendum: Deep-Dive Checks (per team-lead request)

### B1. Candidate A: Push Frequency and Feasibility

**Large push frequency on TMFD6** (tick-to-tick moves > N sigma):

| Regime | >2 sigma | >3 sigma | >5 sigma |
|--------|---------|---------|---------|
| Jan 28 (wide spread) | 8/hr | 3/hr | 1/hr |
| Mar 19 (tight spread) | 843/hr | 213/hr | 24/hr |

The Jan/Mar difference is because Jan has 98.9% of ticks with zero mid-price change (BidAsk updates without price movement), so single-tick pushes are rare but large. March has 36.7% nonzero returns and more granular movement.

**Cumulative pushes (rolling window moves > 2 sigma)**:

| Window | Jan (wide) | Mar (tight) |
|--------|-----------|-------------|
| 10 ticks | 338/hr | 2,233/hr |
| 50 ticks | 601/hr | 1,983/hr |
| 100 ticks | 757/hr | 2,076/hr |

Push-response analysis operates on cumulative moves, not single-tick. At 50-100 tick windows, hundreds of >2 sigma pushes per hour -- ample frequency for a push-response strategy.

**Minimum reversion needed**: At 4.0 pts RT cost (survey estimate), need conditional response > 4.0 pts. Cumulative 50-tick moves have sigma=4.1 pts (Jan) and 6.1 pts (Mar). A 2-sigma push = ~8-12 pts. If mean reversion captures 40-50% of the push, that is 3.2-6 pts gross -- marginal to profitable.

**RTT feasibility**: Push detection happens AFTER the push (by definition). The trader observes the push, then posts a contrarian limit order. At 36ms RTT, the order arrives within 1-3 ticks of the push completing. The push-response paper shows responses develop over hundreds of ticks -- plenty of time. **Signal half-life vs RTT is not a constraint here.**

### B2. Candidate B: Detailed Data Sufficiency

**Trade records**: TMFD6 npy files contain **only BidAsk snapshots** (volume=0 in all rows). No trade records exported. Trade-OFI features (the #1 most important feature per the Bieganowski paper) **cannot be computed** from this data. A separate trade record export from ClickHouse would be required.

**CatBoost sample requirements** (at 1-second bars, ~18,000/day):

| Scenario | Days | Bars | Walk-Forward | Assessment |
|----------|------|------|-------------|------------|
| 14 days (exported) | 14 | ~252K | 10 IS + 4 OOS | Barely viable, high overfit risk |
| 58 days (claimed) | 58 | ~1.04M | 40 IS + 18 OOS (3 folds) | Adequate |

With 20+ features, 14 days is marginal. 58 days would be adequate but only 14 are exported.

**Feature pipeline latency** (20+ features within 250us budget):

Estimated total: ~35us for all 20+ features (6 existing in FeatureEngine + 14 new). Well within 250us budget at 86% margin remaining. L5 features add ~8-16us for array access but still feasible. **Latency is not the blocker.**

**Key blockers for Candidate B**: (1) No trade data in exported files -- trade-OFI is the #1 feature and cannot be computed. (2) No L5 data exported -- multi-level depth features are the #2 feature category. The two most important feature categories are both missing from the research data.

### B3. Candidate C: Jan/Feb Data Interpretation (Regime vs Artifact)

Team-lead flagged that Jan/Feb had median bid/ask qty=40 (vs March qty=4). My verification shows:

- Jan 28: median bid_qty = **5** (not 40), mean = 19.6 (skewed by large outliers)
- Mar 19: median bid_qty = 4, mean = 4.6

The median queue depth difference (5 vs 4) is modest, suggesting the Jan/Feb data IS genuine L1 best-level data, not aggregated multi-level data. The mean of 19.6 in Jan is driven by occasional large queue sizes (up to hundreds of contracts), not systematic aggregation. If the data were L5-aggregated, we would expect median ~20-50, not 5.

The 10-20x regime effect in signal strength (IC 0.19 wide vs 0.007 tight) is likely real and driven by:
- **Wider spreads create more room for timing value** (28 pts spread = more pts available when crossing optimally)
- **Lower tick rate in wide regime** (4.5/sec vs 11.3/sec) = more time to react = better timing accuracy
- **Queue depth differences are minor** (5 vs 4 median) and do not explain the signal strength gap

**Conclusion**: This is NOT a data artifact. The regime effect is a genuine market microstructure phenomenon tied to spread width, not data encoding.

### B4. Architecture Compatibility Ranking

| Candidate | FeatureEngine | StrategyRunner | RingBufferBus | New Components | Score |
|-----------|--------------|---------------|--------------|---------------|-------|
| C: Spread Regime | Uses existing features [3,14] | Standard strategy | No change | ~0 new LOC | **Best** |
| A: Push-Response | 1-2 new features | Standard strategy | No change | Push detector (~40 LOC) | Good |
| B: CatBoost | 14+ new features | Custom inference path | No change | Feature pipeline + model serving (~1,300 LOC) | Poor |

Candidate C is most compatible with existing platform architecture, requiring zero new infrastructure. Candidate A requires minimal additions. Candidate B requires the most new infrastructure and introduces a ML model serving dependency that does not currently exist in the platform.
