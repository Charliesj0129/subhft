# Round 16 Survey V2: Challenger Review

**Date**: 2026-03-26
**Reviewer**: Challenger Agent
**Document Under Review**: `docs/alpha-research/round16_survey2_updated_constraints.md`

---

## Verdict: CONDITIONAL APPROVE (A only), REJECT (B and C)

- Candidate A (Push-Response): CONDITIONAL APPROVE -- genuinely novel direction, quick to validate
- Candidate B (CatBoost): REJECT -- overfitting trap with insufficient data
- Candidate C (Spread Regime): REJECT -- the "regime" is almost certainly a contract maturity artifact, not a predictable microstructure phenomenon

---

## Candidate A: Push-Response Anomalies -- CONDITIONAL APPROVE

### Challenge A.1: SPY Tick Frequency vs. TMFD6 (MAJOR)

The Vlasiuk & Smirnov paper finds push-response anomalies at lag ranges of 1,500-5,000 ticks on SPY. SPY trades at roughly 10,000-50,000 events/second (NBBO updates). At that rate:
- 1,500 ticks = 0.03-0.15 seconds
- 5,000 ticks = 0.1-0.5 seconds

TMFD6 trades at 1.8 ticks/second. At that rate:
- 1,500 ticks = 833 seconds = **14 minutes**
- 5,000 ticks = 2,778 seconds = **46 minutes**

The push-response effect on SPY is a sub-second to second-scale phenomenon. On TMFD6, the equivalent tick range spans nearly the entire trading session. The temporal dynamics of liquidity replenishment operate on wall-clock time (humans and algorithms reacting to price shocks), not tick-time. So the effect, if it exists on TMFD6, would appear at a MUCH lower tick lag -- perhaps 5-50 ticks (3-28 seconds).

This is not fatal but means the paper's lag-range findings cannot be directly transplanted. The quick validation must scan a wide range of lag values.

**Resolution required**: When computing the push-response map, scan lags from 1 to 500 ticks (0.5s to 4.5 minutes) rather than the paper's 1,500-5,000 range.

### Challenge A.2: "Large Pushes" on TMFD6 May Be Informational (MODERATE)

On SPY, large pushes are often caused by temporary liquidity imbalances (large institutional orders, ETF arbitrage flows) that revert because the fundamental value hasn't changed. SPY's massive participant base ensures rapid liquidity replenishment.

On TMFD6, large pushes may be caused by:
- Genuine information arrival (macro events, TWSE index movements)
- Fat-finger errors (rare but impactful on thin books)
- TXFD6 lead-lag effects (TMFD6 follows the main contract)

If large pushes on TMFD6 are primarily informational (not transient pressure), the mean-reversion assumption fails. The push-response map will show this directly -- if conditional responses are zero or same-sign as pushes, information dominates.

This is testable in the quick validation phase, so it's not a blocker.

### Challenge A.3: Cost Viability Threshold

At 4.0 pts RT cost and median spread of 3 pts (March), a push-response trade using passive entry needs:
- Entry: passive limit (saves half-spread ~1.5 pts)
- Exit: passive limit (saves another ~1.5 pts)
- Net cost: 4.0 - 1.5 - 1.5 = ~1.0 pts if both sides fill passively

The conditional response after a large push must exceed 1.0 pts (best case, both passive) to 4.0 pts (worst case, one or both taker). Given that MC-7 showed median 5s move = 3.5 pts and 10s move = 5.0 pts on TMFD6, a conditional response of >4 pts after a 2-sigma push is plausible but needs empirical confirmation.

### Candidate A Verdict: CONDITIONAL APPROVE

This is the first genuinely novel direction in Round 16. It has not been tried in any previous round. The quick validation (push-response map) is cheap to compute and will give a clear kill/proceed signal. The key condition is that the lag range must be adapted for TMFD6's tick frequency.

---

## Candidate B: Multi-Feature CatBoost -- REJECT

### Challenge B.1: 58 Days is Insufficient for Gradient Boosting (CRITICAL)

The survey proposes training CatBoost with ~20 features on 58 days of data. This is a textbook overfitting scenario:

- 58 days = 58 independent samples at the daily level. Walk-forward validation with 40 train / 18 test gives only 18 OOS evaluation points.
- CatBoost with 20 features and default depth (6) has thousands of effective parameters. Even with regularization, 40 training days is grossly insufficient for robust generalization.
- The Bieganowski & Slepaczuk paper used crypto markets with 24/7 trading, giving them effectively 7x more data per calendar day than TMFD6's 5-hour session.

Standard ML practice requires at least 10x more training samples than features per tree. With 20 features and depth 6, we need thousands of independent samples, not 40.

### Challenge B.2: Crypto Transferability is Weak (MAJOR)

The survey cites cross-asset portability of features across BTC/LTC/ETC/ENJ/ROSE as evidence they would transfer to TMFD6. But these are all cryptocurrencies trading on the same exchange (likely Binance), with:
- Same market microstructure (continuous LOB, maker rebates)
- Same participant base (crypto retail + algo traders)
- High cross-correlation (altcoins follow BTC)
- Same fee structure

TMFD6 is structurally different in every dimension: session-based trading, no maker rebates, different participant mix (institutional hedgers + retail), different tick dynamics (1.8/sec vs hundreds/sec for crypto), and different information environment (follows TWSE cash market).

"Features transfer across similar assets on the same exchange" does not imply "features transfer across asset classes on different exchanges."

### Challenge B.3: TLOB Paper Self-Contradicts

The survey itself notes that TLOB "performance deteriorates when trends are defined using average spread" and the authors "acknowledge that translation to profitable strategies is the hard part." If the state-of-the-art transformer model for LOB prediction can't produce profitable strategies, there's no reason to believe a simpler CatBoost on far less data would succeed.

### Candidate B Verdict: REJECT

Too little data, too many features, wrong asset class for transferability claims. The overfitting risk is extreme. If we want to pursue ML-based LOB prediction, we need at minimum 6-12 months of TMFD6 tick data first.

---

## Candidate C: Spread Regime Prediction -- REJECT

### Challenge C.1: Jan/Feb vs March is a Contract Maturity Artifact, Not a Regime (FATAL)

This is the most important challenge in the entire review.

The survey treats the Jan/Feb vs March microstructure difference as a "spread regime" that can be predicted and exploited. The data shows:

| Metric | Jan/Feb | March |
|--------|---------|-------|
| Median spread | 34 pts | 3 pts |
| Bid qty median | 26 contracts | 3 contracts |
| IC at 60s | 0.193 | 0.007 |

The team lead's background analysis found even more extreme values: Jan/Feb median qty=40, spread=38.

This is almost certainly **NOT a predictable microstructure regime**. It is a **contract maturity effect**:

1. **TMFD6 is a futures contract with monthly expiry.** In January and February, the data likely includes far-month contracts (e.g., March or April delivery) that are less liquid and have wider spreads. In March, the data is the near-month (March delivery) which is the most actively traded.

2. **The 10x difference in bid/ask quantities (26 vs 3) is a dead giveaway.** If this were a volatility regime, you'd expect spread to widen but queue depth to remain similar or increase (as market makers widen quotes but maintain size). A simultaneous 10x spread widening AND 8x queue deepening is characteristic of a different contract month, not a different trading regime.

3. **Alternative explanation: the Jan/Feb data may be for a DIFFERENT Shioaji symbol.** TMFD6 is a continuous futures symbol in some broker systems. If Jan data captures the January contract (thin and about to roll) while March data captures the March contract (front month and liquid), the "regime" is just "we're looking at different contracts."

4. **The "99.2% of ticks have spread >= 4" in Jan/Feb is abnormal.** On actively traded index futures, median spread is almost always 1 tick. A median spread of 34 pts when tick size is 1 pt means the market is either (a) extremely illiquid (far-month contract), (b) in pre/post-market session, or (c) the data is aggregated differently.

### Challenge C.2: If It IS a Contract Maturity Effect, Candidate C's Entire Thesis Collapses

The survey proposes predicting "when the spread will be wide or narrow." But if the Jan/Feb wide spread is simply because we're trading the wrong contract month (or a less liquid delivery), then:

- **There's nothing to predict.** The spread regime is determined by which contract month you're trading. You already know this at the start of the session.
- **The 10-20x signal strength difference is illusory.** The imbalance signal appears stronger in Jan/Feb because the wider spread creates larger absolute price moves for the same informational content. But you can't trade this: by the time the spread is wide, you're in the wrong (illiquid) contract.
- **The "blended estimate (50% wide-spread days)" in MC-789 is nonsensical.** If wide spreads are the far-month contract, they don't occur 50% of trading days -- they occur 100% of the time on the far-month contract and 0% on the near-month.

### Challenge C.3: Even If It's a Real Regime, Autocorrelation is Trivial

The quick validation proposes testing 5-minute spread autocorrelation. Spread autocorrelation is almost always >0.9 at 5-minute lags -- this is a well-known property of all LOB data. High autocorrelation does not mean the regime is exploitable:

- Spread autocorrelation tells you "the spread was wide 5 minutes ago, so it's probably still wide." This is trivially true and not actionable for alpha.
- What matters is whether you can predict TRANSITIONS: "the spread will widen in the next 5 minutes when it's currently narrow." This is much harder and the survey provides no evidence this is feasible.

### Candidate C Verdict: REJECT

The Jan/Feb vs March difference is almost certainly a contract maturity or symbol artifact, not a predictable regime. Before any further analysis of this "regime," the researcher must:

1. Confirm what TMFD6 symbol maps to in each month (near-month? specific delivery?)
2. Check if the Jan/Feb data is the same contract month as March data
3. If different contract months, the entire spread regime thesis is dead
4. If same contract month, explain why microstructure changes 10x between months

---

## Summary

| Candidate | Verdict | Key Issue |
|-----------|---------|-----------|
| A: Push-Response | **CONDITIONAL APPROVE** | Genuinely novel. Must adapt lag range for TMFD6 tick frequency. Quick validation is cheap. |
| B: CatBoost ML | **REJECT** | 58 days insufficient for gradient boosting with 20 features. Crypto transferability claim is weak. |
| C: Spread Regime | **REJECT** | Jan/Feb vs March difference is almost certainly a contract maturity artifact, not a predictable regime. |

### Mandatory Conditions for Proceeding

1. **Candidate A**: Compute push-response map on TMFD6 with lag range 1-500 ticks (not 1,500-5,000). If conditional response after 2-sigma push > 2 pts at any lag, proceed to prototype. If < 1 pt at all lags, kill.

2. **Candidate C data investigation**: Before ANY further analysis, confirm what contract month the Jan/Feb TMFD6 data represents. If it's a different contract month from March, close this direction permanently. This should take 10 minutes of data inspection, not a research phase.

### Broader Observation

The survey correctly identifies that directional prediction on TMFD6 is dead (Rounds 12-15 established this). The pivot to execution optimization and conditional mean-reversion (Candidate A) is the right strategic direction. But Candidate C builds its thesis on a likely data artifact, and Candidate B is premature without sufficient data for proper ML training.

---

## ADDENDUM: Two Additional Candidates from Survey V2 (`round16_stage1_survey_v2.md`)

### Candidate #1 (Survey V2): Order-Flow Entropy -- CONDITIONAL APPROVE

#### Challenge E.1: Trade Direction Inference from L5 Snapshots is a Degraded Proxy (MAJOR)

The Singha paper computes its 15-state Markov transition matrix from **individually classified trades** (buy-initiated vs. sell-initiated) at 1-second resolution on SPY. SPY has actual trade-level data with aggressor-side flags via TAQ/ITCH.

Our TMFD6 data is **L5 quote snapshots**, not individual trades. The survey acknowledges we need to "infer trade direction from quote changes." This is a fundamental data quality downgrade:

- **Lee-Ready tick rule** on quote changes has ~85-90% accuracy on liquid US equities (Chakrabarty et al. 2007), but this accuracy was measured on instruments with much higher tick rates. On TMFD6 at 1.8 ticks/sec, each "tick" may aggregate multiple trades, making direction inference noisier.
- **The 15-state Markov matrix amplifies classification errors.** If individual trade direction is wrong 15% of the time, transition probabilities in the 15x15 matrix accumulate errors multiplicatively. A state sequence of 5 trades has ~(0.85)^5 = 44% chance of being fully correct.
- **This is the SAME blocker that killed Hawkes Intensity, ISS features, and Candidate B (Toxic Flow) in previous rounds.** The Execution Review flagged missing trade-side classification as a recurring infrastructure gap. The survey proposes to work around it with heuristics, but the signal-to-noise ratio of entropy computed from misclassified trades is unknown.

**Resolution required**: Before computing entropy, validate the trade direction inference quality. Classify TMFD6 quote changes using tick rule, then compute "known-direction entropy" (using L5 bid/ask changes where direction is unambiguous, e.g., bid increases = buy, ask decreases = sell) vs. "inferred-direction entropy." If the correlation between the two entropy series is < 0.7, the proxy is too noisy and the candidate dies.

#### Challenge E.2: Magnitude Prediction Without Direction Has Limited Actionability (MODERATE)

The survey correctly notes this is "not directly tradable" since entropy predicts magnitude, not direction. The proposed applications are:

1. **Execution timing**: Defer trading during low-entropy (large-move-expected) periods
2. **Volatility trading**: Size positions based on expected volatility

But consider the practical implications:

- **Execution timing (defer during low entropy)**: This is defensive. It tells you WHEN NOT to trade. If low-entropy periods are 10-20% of the session, you're already avoiding them naturally by only trading when your other signals fire. The incremental value of an explicit entropy filter depends on how much overlap exists between "low entropy" and "other signals say trade."
- **Volatility trading**: We don't have a volatility instrument. We can't trade implied vol or VIX equivalent. "Increase position sizing during high expected vol" requires a DIRECTIONAL signal first -- vol sizing multiplies alpha, it doesn't create it.

The strongest use case is combining entropy with the Push-Response signal (Candidate #2 / A): use entropy to identify low-entropy regimes where large moves are expected, then use push-response to trade the subsequent reversion. This is a valid composition, but it requires BOTH signals to work independently first.

#### Challenge E.3: "58 Days Sufficient" Claim is Arguable

The survey claims "Singha validated on 36 days. We have 58." But Singha validated on SPY which has ~390,000 trades/day. In 36 days, that's ~14M classified trades. TMFD6 at 1.8 ticks/sec over a 5-hour session has ~32,400 ticks/day. In 58 days, that's ~1.88M ticks. The per-tick information content is different, but the number of independent entropy windows (at 30-second resolution) is:

- Singha: 36 days * 6.5 hours * 120 windows/hour = 28,080 windows
- Ours: 58 days * 5 hours * 120 windows/hour = 34,800 windows

This is comparable, so the claim holds for entropy computation. However, the statistical validation (quintile split + conditional return measurement) needs enough data in each quintile, which we have.

#### Candidate #1 Verdict: CONDITIONAL APPROVE

The entropy concept is sound and novel for our context. It fills a genuinely different niche (magnitude prediction) compared to everything we've tried. The main risk is trade direction inference quality, which is testable in the quick validation phase. If the proxy entropy correlates >0.7 with "clean" entropy, proceed.

---

### Candidate #3 (Survey V2): Closing Auction Effects -- REJECT

#### Challenge CA.1: 1-2 Trades Per Day is Below Viability Threshold (CRITICAL)

The survey acknowledges this produces "at best 1-2 trades per day." Let me quantify the expected PnL:

- TMFD6 half-spread near close: assume compression to ~1.5 pts (optimistic)
- Edge per closing trade: if we capture spread compression of 1-2 pts, net of 4.0 pts RT cost, the PnL is NEGATIVE
- Even if we achieve a directional edge from anticipated clearing price, at 1-2 trades/day, we need >4 pts per trade net to produce meaningful PnL
- 1-2 trades/day * 4 pts/trade * 10 NTD/pt = 40-80 NTD/day = **9,800-19,600 NTD/year**

This is not worth the engineering investment to build, test, and maintain a closing-auction-specific strategy.

#### Challenge CA.2: TMFD6 Closing Auction is Almost Certainly Too Thin

The survey notes this risk but doesn't quantify it. TMFD6 is the Mini-TAIEX futures contract. The closing auction for index futures is designed primarily for the main TX contract where institutional hedgers need to settle. TMFD6's closing call auction likely has:
- Minimal institutional participation (institutions use TX, not TMF)
- Low volume (retail traders don't systematically target the closing auction)
- Wide spreads during the auction convergence period

The Korean market analogy (Kang 2025) is about **stock market** institutional buying, not futures closing auctions. The mechanisms are different: stock market has closing cross with mandatory MOC orders from index funds; futures market has much less structural flow at close.

#### Challenge CA.3: Competing with Established Closing Auction Algos

Even if TMFD6's closing auction has exploitable dynamics, this is one of the most well-studied effects in market microstructure. Every institutional VWAP/TWAP algorithm already optimizes for session-end dynamics. A retail trader with 36ms latency trying to front-run institutional closing flow is bringing a knife to a gunfight.

#### Candidate #3 Verdict: REJECT

Too few trades per day, uncertain auction liquidity on TMFD6, and competing against established institutional algorithms. The engineering cost is not justified for <20,000 NTD/year expected value.

---

## Updated Summary Table

| Candidate | Source | Verdict | Key Issue |
|-----------|--------|---------|-----------|
| A: Push-Response (Survey V2 #2) | Previous review | **CONDITIONAL APPROVE** | Adapt lag range for TMFD6 frequency |
| B: CatBoost ML | Previous review | **REJECT** | 58 days insufficient, crypto transferability weak |
| C: Spread Regime | Previous review | **REJECT** | Contract maturity artifact |
| #1: Order-Flow Entropy | Survey V2 | **CONDITIONAL APPROVE** | Trade direction inference quality is the gate |
| #3: Closing Auction | Survey V2 | **REJECT** | 1-2 trades/day, thin auction, < 20K NTD/year |

**Two survivors**: Push-Response (A) and Order-Flow Entropy (#1). These are complementary -- entropy identifies WHEN large moves occur, push-response identifies HOW to trade the reversion after them. If both validate, they compose into a single strategy: detect low-entropy regime -> wait for large push -> post contrarian passive limit -> capture reversion.
