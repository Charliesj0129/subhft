# Round 16 Survey V2 (Updated): Execution Review

**Reviewer**: Execution Agent
**Date**: 2026-03-26
**Document reviewed**: `docs/alpha-research/round16_stage1_survey_v2.md`

---

## Candidate #1: Order-Flow Entropy as Volatility Regime Detector

### Trade Direction Inference from L5 Snapshots: MAJOR CONCERN

The entropy computation requires trade-level buy/sell classification. Our TMFD6 data contains **only BidAsk L1 snapshots** (volume=0 in all rows, no individual trade records).

I tested trade direction inference from quote changes on the actual data:

| Regime | Classifiable | Unclassifiable | Mid-price changes |
|--------|-------------|---------------|-------------------|
| Mar 19 (tight, 3 pts spread) | 49.4% | 50.6% | 36.7% |
| Jan 28 (wide, 28 pts spread) | 1.2% | 98.8% | 1.1% |

**In the wide-spread regime (Jan/Feb), 98.8% of quote updates are unclassifiable.** Only 1.1% of ticks have mid-price changes, and only 0.6% can be classified as buy/sell. This renders entropy computation meaningless in the wide-spread regime -- the 15-state Markov matrix would be almost entirely "no trade" states.

In the tight-spread regime (March), ~49% classifiable is workable but still means half the data is lost. The paper's 15-state model uses trade-level data with 100% classification (SPY has NBBO + trades). Our quote-change proxy is a fundamentally lower-quality input.

**Can we get trade records from ClickHouse?** The `hft.market_data` table may contain tick (trade) events separately from BidAsk events. The existing export (`ch_batch_export`) only exported BidAsk. A separate trade record export would resolve this -- but this is an engineering prerequisite, not a quick validation.

### Signal Applicability: CONCERN

Entropy predicts magnitude, not direction. This is useful for:
- Execution timing (defer during low entropy = large move coming)
- Volatility sizing
- Risk management

But it does NOT generate alpha on its own. It must be combined with a directional or mean-reversion signal. The survey acknowledges this ("Not directly tradable... Needs combination with another signal") but all our directional signals are dead. Entropy becomes actionable only if paired with Candidate #2 (push-response) or a new directional approach.

### Feature Compatibility: MEDIUM

New features required:
1. Trade direction classifier (from quote changes) -- ~80 LOC, requires per-tick state
2. 15-state Markov transition counter (rolling 30-60s window) -- ~120 LOC, pre-allocated 15x15 matrix
3. Entropy computation from transition matrix -- ~30 LOC, single scalar output

Total ~230 LOC. The Markov matrix is a 15x15 int array = 900 bytes, fits in L1 cache. Entropy computation is O(225) multiplications per update -- negligible latency (<1us). **Hot-path safe.**

### Candidate #1 Verdict: CONDITIONAL APPROVE

**Conditions:**
1. Must first verify whether ClickHouse has separate trade records for TMFD6. If yes, export them. If no, the quote-change proxy is too lossy (98.8% unclassifiable in wide-spread regime).
2. Must be paired with a directional/mean-reversion signal (Candidate #2). Not standalone.
3. Quick validation should use March data only (where 49% is classifiable), not Jan/Feb.

---

## Candidate #2: Push-Response Conditional Mean-Reversion

### Push Frequency: PASS (well above 5/day threshold)

Measured 2+ sigma 30-second cumulative moves on TMFD6:

| Day | 2-sigma threshold | >2 sigma events | Per hour | Per 5-hr day (est) |
|-----|------------------|----------------|----------|-------------------|
| Jan 28 (wide) | 14.6 pts | 216 | 10.5 | ~53 |
| Jan 30 (wide) | 25.4 pts | 389 | 19.0 | ~95 |
| Mar 19 (tight) | 35.2 pts | 273 | 21.4 | ~107 |

**All scenarios produce 53-107 events per day**, far exceeding the 5/day minimum threshold. Push frequency is NOT a constraint.

Note: the 2-sigma threshold varies by regime (14.6 pts wide vs 35.2 pts tight). In the wide-spread regime, pushes are smaller in absolute terms but more frequent relative to sigma, because baseline volatility is lower.

### Minimum Reversion Size: ACHIEVABLE

At 4.0 pts RT cost (survey estimate, conservative), need conditional response > 4.0 pts.

| Regime | 2-sigma push size | 50% reversion | 30% reversion | vs 4 pts cost |
|--------|------------------|---------------|---------------|---------------|
| Jan (wide) | ~14.6 pts | 7.3 pts | 4.4 pts | Profitable at 30%+ |
| Jan 30 (wide) | ~25.4 pts | 12.7 pts | 7.6 pts | Profitable at 30%+ |
| Mar (tight) | ~35.2 pts | 17.6 pts | 10.6 pts | Profitable at 30%+ |

Even at 30% partial reversion (conservative), all regimes produce conditional responses > 4 pts for 2-sigma pushes. **Cost barrier is clearable.**

### RTT Feasibility: PASS

Push detection is backward-looking (observed after the fact). The trader then posts a contrarian limit order. At 36ms RTT vs 30-second push windows, there is no latency constraint. The response develops over minutes. Signal half-life (30s-5min) provides 800-8,000 half-lives of margin over RTT.

### Infrastructure Feasibility: PASS

- Standard `StrategyRunner` handles the "detect push, post limit, exit on reversion" logic.
- Uses existing `mid_price_x2` [2] and `spread_scaled` [3] from FeatureEngine.
- New features: rolling 30s return tracker (~40 LOC), sigma estimator (~20 LOC).
- Total: ~60 LOC of new features. Trivial hot-path cost.

### Candidate #2 Verdict: APPROVE

This is the strongest candidate across both surveys. Novel direction, empirically testable, sufficient push frequency, favorable cost economics, minimal implementation cost. Quick validation (conditional return analysis after 2-sigma pushes) is straightforward and can be done in 1-2 hours.

---

## Candidate #3: Closing Auction Market-Making

### Does TMFD6 Have a Closing Auction? UNCERTAIN

TAIFEX uses continuous matching for index futures during regular hours. I analyzed session-end behavior:

**Jan 28 (has both day and night session data):**

| Period | Tick rate | Median spread | Mean spread |
|--------|----------|---------------|-------------|
| Day mid (10:00-12:00) | 4.9/sec | 33 pts | 36.5 pts |
| Day close (13:15-13:45) | 5.1/sec | 17 pts | 19.2 pts |
| Night mid (20:00-23:00) | 5.1/sec | 26 pts | 29.4 pts |
| Night close (04:30-05:00) | 3.0/sec | 36 pts | 43.9 pts |

**Observations:**
- **Day session close**: Spread NARROWS (17 vs 33 pts mid-session). Tick rate stable. This is consistent with closing auction convergence effects.
- **Night session close**: Spread WIDENS (36 vs 26 pts mid-session). Tick rate DROPS (3.0 vs 5.1). This suggests liquidity withdrawal at night close, not auction convergence.
- The day vs night session closing behavior is OPPOSITE, suggesting different mechanisms at play.

**March 19 (night session only):**

| Period | Tick rate | Median spread |
|--------|----------|---------------|
| Night mid (20:00-23:00) | 14.8/sec | 3 pts |
| Night close (04:30-05:00) | 3.7/sec | 3 pts |

In March tight-spread regime, night close shows tick rate dropping (14.8 -> 3.7) with stable spread. This is simple liquidity withdrawal, not auction dynamics.

### Key Findings

1. **Day session close shows spread compression** (17 vs 33 pts in Jan) -- possible closing auction effect. But we have NO March day session data to confirm this is persistent.
2. **Night session close shows liquidity withdrawal** -- opposite of auction dynamics. NOT tradable.
3. **Only 1-2 trades per day** at session boundaries -- extremely low frequency.
4. The paper's approach (Deep Q-Learning) requires RL infrastructure we do not have.
5. **Korean market analogy is STOCKS, not futures**. The institutional must-trade dynamic is driven by equity benchmark tracking, which does not apply to TMFD6 futures.

### Candidate #3 Verdict: REJECT

**Reasons:**
1. Night session close shows liquidity withdrawal, not auction convergence -- opposite of the paper's premise.
2. Day session close data is available for only ~2 days (Jan 28-30). Insufficient to validate.
3. Extremely low frequency (1-2 trades/day at best).
4. Korean market analogy does not transfer to TAIFEX futures.
5. RL infrastructure requirement is out of scope.

---

## TXO Options Data (33M Rows) -- Feasibility Assessment

### Platform Readiness

TXO options are defined in `config/symbols.yaml` (e.g., `TXO33500P6`, `TXO34400D6`). The platform has:
- Symbol definitions with `product_type: option`
- Tags: `options`, `txo`
- Options symbol resolution patterns in `config/symbols.list` (`OPT@TXO@near@ATM+/-10`)

### Potential Use Cases

1. **Put-call ratio as TXFD6/TMFD6 directional signal**: Classic cross-instrument signal. Requires real-time options quote ingestion.
2. **Implied volatility skew changes**: Skew shifts predict futures moves. Well-established but latency-sensitive.
3. **Unusual options activity detection**: Large options trades signal informed trading. Could feed into entropy-style regime detection (Candidate #1).
4. **Options-implied density for execution optimization**: Use options-implied distribution to set optimal limit order distances.

### Feasibility Assessment

| Factor | Status |
|--------|--------|
| Data exists | YES (33M rows in ClickHouse) |
| Symbol config | YES (TXO defined) |
| Real-time ingestion | UNKNOWN -- need to verify Shioaji/Fubon options quote support |
| Options pricing model | NOT BUILT -- need BSM or similar for IV computation |
| Cross-instrument latency | CONCERN -- options quote + compute IV + signal -> futures order = multi-step pipeline |
| Data quality | UNKNOWN -- 33M rows across how many strikes/expiries/days? |

### TXO Verdict: DEFER (engineering investigation, not research)

The TXO data is a legitimate opportunity but requires **engineering work** before research can begin:
1. Verify ClickHouse data schema (what fields? which strikes/expiries?)
2. Build IV computation module (offline first, then real-time)
3. Verify broker API supports options quote subscription
4. Assess cross-instrument latency budget

This is NOT a Stage 2 alpha candidate. It is a **data infrastructure project** that could enable future alpha research in Rounds 17+.

---

## Overall Assessment

| Candidate | Verdict | Confidence | Priority |
|-----------|---------|------------|----------|
| #2: Push-Response | **APPROVE** | High | 1st (quick validation immediately) |
| #1: Order-Flow Entropy | **CONDITIONAL APPROVE** | Medium | 2nd (blocked on trade data export) |
| #3: Closing Auction | **REJECT** | High | N/A |
| TXO Options | **DEFER** | Medium | Engineering backlog |

### Config Drift (Updated)

| Item | Survey Claim | Verified | Drift |
|------|-------------|----------|-------|
| TMFD6 data rows | 9.16M | 5.14M exported | DRIFT (same as previous review) |
| Days | 58 | 14 exported | DRIFT |
| L5 depth | Available | Not in npy files | DRIFT |
| Trade records | Needed for #1 | Not in npy files (volume=0) | DRIFT -- need separate export |
| Tick rate | 1.8/sec | 4.5-11.3/sec | DRIFT (actual higher) |
| TMFD6 closing auction | Assumed present | Uncertain -- day close shows compression, night shows withdrawal | UNCERTAIN |

### Recommended Next Steps

1. **Immediate**: Quick validation of push-response on TMFD6 (Candidate #2). Compute conditional 60s/120s/300s returns after 2+ sigma 30s pushes. Test asymmetry (negative vs positive pushes). Estimate net PnL after 4 pts cost.

2. **Parallel**: Check ClickHouse for TMFD6 trade records (`SELECT count() FROM hft.market_data WHERE symbol='TMFD6' AND event_type='tick'` or similar). If trade records exist, export and retry Candidate #1 entropy validation.

3. **Deferred**: TXO data investigation as engineering task.
