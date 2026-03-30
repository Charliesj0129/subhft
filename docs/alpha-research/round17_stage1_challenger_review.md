# Round 17 -- Stage 1 Challenger Review

**Date**: 2026-03-26
**Role**: Challenger
**Scope**: Challenge EGVT, OIDS, FPOPE candidates from R17 Stage 1 Survey

---

## Candidate 1: EGVT (Entropy-Gated Volatility Timing)

### Challenge 1: Entropy Already Failed on TMFD6 (R16 Data)

**This is a critical challenge.** Round 16 explicitly tested order-flow entropy on TMFD6 across 4 days and **all 4 days FAILED** (see `round16_entropy_validation_results.md`):

- March 19: Q1/Q5 ratio = 0.904 (60s), 0.977 (300s) -- **FAIL**
- March 20: Q1/Q5 ratio = 1.060 (60s), 0.974 (300s) -- **FAIL**
- Jan 30: **Degenerate** -- 59% of seconds have zero entropy, Q1-Q2 empty
- Feb 04: **Degenerate** -- same as Jan 30

The R16 conclusion was explicit: *"Order-flow entropy does NOT predict move magnitude on TMFD6."* And the root cause analysis identified structural reasons:

1. Trade direction inference from L1 quotes is too lossy (49% classification in tight spread, 1.1% in wide spread)
2. TMFD6 tick rate (~1.8/sec) is 100x lower than SPY (~167/sec), producing severely undersampled transition matrices
3. Wide-spread regime (which is ~45% of trading time) produces degenerate zero-entropy

**The EGVT proposal is a repackaged version of the same signal that already failed.** The only differences are:
- 15-state model (vs 3-state in R16) -- but the execution review itself flags that 216 transitions in 120s across 225 cells produces a "severely undersampled matrix"
- Used as a gate for CBS rather than standalone alpha -- but if the signal has no discriminative power (flat across quintiles), gating on it is gating on noise

**Quantitative question**: Given that the R16 3-state entropy test showed Q1/Q5 ratios between 0.90-1.06 (essentially 1.0 = no signal), what evidence suggests that moving to 15 states -- which makes the undersampling problem 25x worse (225 cells vs 9 cells) -- will produce a stronger signal?

### Challenge 2: Tick-Rate Sampling Budget Is Mathematically Insufficient

The execution review flags this but does not follow through to its logical conclusion. Let's do the math:

- TMFD6 tick rate: 1.8/sec
- 120-second window: 216 ticks
- 15-state transition matrix: 15x15 = 225 cells
- **Expected observations per cell: 216/225 = 0.96**

This means on average, each cell has less than 1 observation. Shannon entropy computed from a matrix where most cells are 0 or 1 is dominated by sampling noise, not by the underlying process. Even at 600 seconds (1080 ticks), expected observations per cell = 4.8, which is still far below statistical reliability.

For comparison, the Singha et al. paper on SPY uses ~6000 trades per 120-second window (SPY trades ~50/sec). That gives 6000/225 = 26.7 observations per cell. TMFD6 has 28x less data per window.

**Quantitative question**: What is the minimum number of observations per cell needed for reliable Shannon entropy estimation? (Answer from information theory: typically > 10-20 per cell for reasonable bias-variance tradeoff.) At TMFD6's tick rate, this requires a window of 225 * 15 / 1.8 = ~1875 seconds = **31 minutes**. At that timescale, the entropy gate responds far too slowly to gate a 300-second CBS strategy.

### Challenge 3: The "Gate" Framing Masks Lack of Signal

Repositioning a failed directional signal as a "volatility gate" does not rescue it. For a gate to work, it must satisfy:

1. The gated metric (entropy) must have discriminative power between high-volatility and low-volatility regimes
2. The gate must reduce false entries more than it eliminates true entries

R16 showed entropy is flat across return quintiles. If entropy does not discriminate between regimes that produce different return magnitudes, gating on it will randomly suppress CBS entries with no systematic improvement. Expected outcome: CBS win rate unchanged, but fewer trades (lower total PnL).

### Verdict: REJECT

**Rationale**: This is the same signal that failed in R16, repackaged with a higher-dimensional model that makes the underlying data insufficiency problem worse. The mathematical sampling budget is 28x below what the reference paper uses. No amount of implementation quality (pre-allocated arrays, process_tick()) can rescue a signal that does not exist in this data.

**Condition for reconsideration**: If the researcher can produce a quantitative backtest showing EGVT-gated CBS has statistically significant improvement over ungated CBS (>2 standard errors, >4 days of data, both tight and wide spread regimes), then re-evaluate. But the prior from R16 is strongly against.

---

## Candidate 2: OIDS (Options-Informed Directional Signal)

### Challenge 1: Paper Transferability -- Market Structure Mismatch

Michael et al. (2022) demonstrate predictability of **overnight US equity returns** from option volume imbalance. There are critical differences with our use case:

1. **Overnight vs. intraday**: The paper's strongest result is overnight return prediction. The survey proposes 5-60 minute intraday horizons, which is a different and weaker signal regime. The paper does show intraday predictability but with much smaller effect sizes.

2. **Market-maker classification**: The paper's key insight is that **market-maker-classified volumes** are the strongest signal. TXO data from Shioaji does not include participant classification. We cannot separate market-maker flow from retail/institutional flow. The strongest signal component is unavailable.

3. **US equity options vs. Taiwan index options**: US equity options are highly liquid with tight spreads across hundreds of strikes. TXO options may have liquidity concentrated in a handful of ATM strikes with wide spreads on OTM options. This limits the signal surface.

4. **Michael et al. use 30-minute and daily aggregation windows.** At TMFD6's 1.8 ticks/sec, the TXO aggregate tick rate across 42 strikes might be 5-20/sec, but most strikes will be extremely illiquid (<1 trade/minute). The volume imbalance signal may be noisy at 5-minute horizons.

**Quantitative question**: Before proceeding, what is the actual TXO trade frequency per strike per day? If the median strike trades <100 times/day, the put/call volume imbalance signal has very few independent observations per intraday window.

### Challenge 2: 33M Rows Does Not Mean 33M Useful Observations

The survey treats "33M TXO rows" as a major asset. But these rows span 42 contracts over ~58 days. That is:

- 33M / 42 contracts = ~786K rows per contract
- 786K / 58 days = ~13.5K rows per contract per day
- Split tick/bidask: if 50% are BidAsk snapshots, useful trade data is ~6.75K per contract per day

For ATM options (where the signal is strongest), perhaps 4-6 contracts are actively traded (near-month ATM calls and puts at 2-3 strikes). That gives ~27K-40K useful trade observations per day across the signal surface.

Furthermore, the data quality is completely unvalidated:
- Are there gaps in the recording? (ClickHouse downtime, subscription drops)
- Do the 42 contracts actually all have data, or are many near-zero volume?
- Are the recorded prices mid-quotes, last-trade, or settlement? This matters for IV computation.

**Quantitative question**: Run `SELECT symbol, count(*), min(exch_ts), max(exch_ts) FROM hft.market_data WHERE symbol LIKE 'TXO%' GROUP BY symbol ORDER BY count(*) DESC LIMIT 20` to understand the actual data distribution before committing to an offline prototype.

### Challenge 3: IV Computation Introduces Its Own Noise

IV extraction via Black-Scholes on illiquid options is notoriously noisy:
- Wide bid-ask spreads on OTM options mean the "price" used for IV computation can swing wildly
- Time-to-expiry near settlement creates extreme vega sensitivity
- Risk-free rate approximation adds systematic bias (minor but present)
- Dividend yield approximation for TAIEX (which pays dividends in July cluster) is non-trivial

The signal is: option volume imbalance predicts direction. But if IV is noisy, the "high-IV options carry more signal" filtering criterion from the paper becomes unreliable, and you lose the strongest signal component.

### Verdict: CONDITIONAL APPROVE

**Conditions**:
1. **Data quality validation MUST come first** -- run the ClickHouse query above. If <50% of configured TXO strikes have meaningful volume (>1000 ticks/day), downgrade confidence significantly.
2. **Start with raw volume imbalance, NOT IV-weighted** -- IV computation adds noise and complexity. Test whether simple put/call volume ratio (no IV) has IC > 0.02 against TMFD6 returns at 15/30/60 min horizons. If raw volume fails, IV-weighted volume is unlikely to rescue it.
3. **Define a clear kill gate**: IC < 0.015 at all horizons on 30+ days of data = drop. No "maybe with better parameters" second chances.
4. **Acknowledge the paper mismatch**: The researcher should explicitly quantify expected signal decay from overnight->intraday and from market-maker-classified->unclassified volumes.

OIDS is the most intellectually promising candidate because it taps a fundamentally different information source (options market). But intellectual promise does not equal realized edge. The offline validation must be rigorous and fast.

---

## Candidate 3: FPOPE (Fill Probability-Optimized Passive Execution)

### Challenge 1: This Is Not Alpha -- It's Execution Optimization

FPOPE does not generate trading signals. It optimizes execution of signals generated by other strategies (CBS). The expected improvement is:

- CBS currently uses market orders (IOC): guaranteed fill, worst price
- FPOPE would switch to limit orders when fill probability is high: better price, risk of no fill
- Expected saving: ~1-2 pts per trade (partial spread capture)

With CBS running ~15 trades/day, that is 15-30 pts/day of potential improvement. At 10 NTD/pt, that is 150-300 NTD/day. This is a real but small improvement.

**The challenge**: This optimization is meaningful ONLY if the base strategy (CBS) is profitable. CBS is currently in shadow with OOS +3.00 bps/trade but confidence interval [-0.76, +7.17] that includes zero. If CBS itself fails shadow validation, FPOPE effort is wasted.

**Quantitative question**: What is the expected FPOPE improvement in bps/trade? If CBS edge is +3.00 bps and FPOPE saves 0.5-1.0 bps, that is a 17-33% improvement. But if CBS edge is actually 0 (within CI), FPOPE saves 0.5-1.0 bps on a breakeven strategy -- still not profitable.

### Challenge 2: Fill Probability Model Has a Chicken-and-Egg Problem

The execution review correctly identifies a critical gap: building the fill probability model requires historical fill data from limit order placements. But we have never placed limit orders on TMFD6 in a systematic way. There is no training data.

Options:
1. **Heuristic v1** (spread > threshold -> market, else limit): This is just a spread-based rule that any trader would apply intuitively. Does it need a "model"? The implementation effort of 680 LOC for a simple if-else seems excessive.
2. **Simulated fill data from L1/L2 snapshots**: Model "would my limit order have filled?" by checking if the market price crossed the limit price. This introduces lookahead bias (you see the cross, but in reality queue position matters).
3. **Learn from live shadow fills**: Requires shadow limit order placement for weeks to build calibration data. Slow and capital-consuming.

**Quantitative question**: For heuristic v1, what is the expected fill rate for limit orders placed at best bid/ask on TMFD6? If the spread is typically 4 pts (1 tick = 1 pt on TMFD6), then a limit order at best bid is immediately undercut by anyone willing to pay 1 pt more. Without queue priority data (which Shioaji does not provide), we cannot estimate fill rates.

### Challenge 3: Limit Order Risk on 300-Second CBS Hold Period

CBS has a 300-second hold period with a 15 bps stop-loss. If the entry order is a limit order that takes 5-30 seconds to fill (or doesn't fill), the effective hold period is shortened and the stop-loss trigger time is delayed. This creates several risks:

1. **Adverse selection on fills**: Limit orders that DO fill are often filled because the market moved against you (the market came to your price, meaning your contrarian bet started underwater). This is exactly the adverse selection problem that makes MM unprofitable at 36ms RTT (R13 finding).
2. **Missed entries on the best trades**: The strongest CBS signals (sharp cascade bounces) are exactly the ones where limit orders are least likely to fill because the price is moving away from your limit quickly.
3. **Execution complexity**: If the limit order doesn't fill within X seconds, do you cancel and switch to market? This adds state machine complexity and introduces the worst-case scenario: paying BOTH the fill probability computation cost AND the full spread.

### Verdict: CONDITIONAL APPROVE (as deprioritized)

**Conditions**:
1. **Defer until CBS shadow results are confirmed profitable** (30+ days of shadow data). Do not optimize execution of a strategy that may not have edge.
2. **Implement as a trivial heuristic first**: If spread >= 2 * median_spread, use limit at mid. If spread == minimum (1 tick), use market. This is 20 lines, not 680.
3. **Do NOT build a "fill probability model" or "execution optimization layer" until the heuristic has been validated** to save >0.5 pts/trade over 100+ trades.
4. **Track adverse selection explicitly**: For every limit order fill, record whether the market moved against the fill direction in the next 10 seconds. If adverse selection rate >60%, limit orders are worse than market orders despite the spread savings.

---

## Survey Completeness Assessment

### Strengths
- Good coverage of arXiv q-fin.TR literature
- Proper rejection of dead zones from R12-R16
- Realistic cost and latency analysis for each candidate
- OIDS represents a genuinely novel direction (cross-instrument information flow)

### Gaps and Missed Directions

1. **No consideration of intraday seasonality/time-of-day effects**: The R14 CBS research found "opening = momentum, rest = mean-reversion." Time-of-day gating for CBS (suppress entries in first 30 minutes, emphasize entries 10:00-12:00) is a zero-infrastructure, zero-data-gap improvement that was not explored. This is the lowest-hanging fruit.

2. **No exploration of volume-weighted or VWAP-relative signals**: The survey searched for microstructure and options signals but did not explore whether TMFD6 price relative to its own VWAP (or volume profile) has predictive power for mean-reversion at the CBS holding period. VWAP reversion is one of the most robust equity effects and requires zero new infrastructure.

3. **No exploration of inter-session (overnight) gap signals**: TMFD6 opening price vs. previous close, overnight index futures movements, etc. These are slow signals that fit the CBS hold period and require only session-boundary data.

4. **FCSM was dismissed too quickly**: The survey gave it "low conviction" but the term structure slope signal operates at exactly the right timescale for CBS (hours). The dismissal was based on "TAIEX term structure is thin" -- but has this been quantitatively verified? How many active TAIEX expiry months are there? What is the historical basis between them?

### Survivorship Bias

Moderate risk. The survey explored ~120 papers but converged on candidates that confirm the researcher's priors (options = novel, entropy = fixable, execution = useful). The rejected candidates table shows appropriate filtering, but missing directions (time-of-day, VWAP, overnight gaps) suggest the search was skewed toward academically novel approaches rather than practically effective ones.

---

## Summary Verdicts

| Candidate | Challenger Verdict | Key Issue |
|-----------|-------------------|-----------|
| **EGVT** | **REJECT** | Entropy already FAILED on TMFD6 in R16. 15-state model makes undersampling 25x worse. Mathematical impossibility at 1.8 ticks/sec. |
| **OIDS** | **CONDITIONAL APPROVE** | Genuinely novel direction, but paper transferability is uncertain. Data quality validation must come first. Start with raw volume, not IV-weighted. |
| **FPOPE** | **CONDITIONAL APPROVE (deprioritized)** | Not alpha. Defer until CBS profitability confirmed. Start with trivial heuristic (20 LOC), not execution layer (680 LOC). |

### Recommended Priority for Stage 2

1. **OIDS offline validation** -- Run TXO data quality check + raw put/call volume IC test. Clear kill gate: IC < 0.015. Effort: 1 session.
2. **CBS time-of-day gating** (NEW) -- Test whether suppressing CBS entries in first/last 30 minutes improves OOS results. Effort: 0.5 sessions. Zero infrastructure cost.
3. **FPOPE heuristic v1** -- After CBS shadow confirms profitability. Effort: 0.5 sessions.
4. **EGVT** -- Drop unless researcher can produce quantitative rebuttal to the R16 failure and the sampling budget math.
