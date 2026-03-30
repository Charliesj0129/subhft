# Round 16 Stage 1 Survey V2: New Directions After First-Survey Failures

**Date**: 2026-03-26
**Context**: All candidates from Survey V1 failed empirical validation. This survey excludes all proven-dead directions and searches for fundamentally different approaches.

---

## Proven Dead (Excluded from Search)

- L1 depth imbalance as directional predictor (IC=0.04, 52% accuracy)
- Spread-conditional maker (adverse selection trap, -8 pts/fill)
- OFI regime-conditional directional signal (0.001 bps ceiling)
- Reversal timing for execution optimization (+0.03 pts, negligible)
- Lead-lag cross-instrument (cost 20x > signal)
- Bidirectional MM at 36ms RTT
- LOB KE/momentum signals

## Instrument and Data

- **TMFD6 (Mini-TAIEX futures)**: 9.16M rows, 58 days, L5 depth, 1.8 ticks/sec
- **Cost**: 40 NTD RT = 4.0 pts = 1.33 bps
- **Also available**: TXFD6 L1 (13 days), 2330/2881 stocks, 33M TXO option rows (ClickHouse)
- **Spread regime**: Jan/Feb median 34 pts (IC=0.19), March median 3 pts (IC=0.007)

---

## Search Methodology

9 searches across q-fin.TR, q-fin.ST, q-fin.CP, q-fin.MF, q-fin.PM, q-fin.PR. Focused on:
1. Order-flow entropy and informed trading detection
2. Push-response / conditional mean-reversion
3. Trend/reversion regime identification
4. Options-informed futures signals
5. Closing auction strategies
6. Volatility/variance strategies
7. VPIN and trade classification

---

## Candidate #1: Order-Flow Entropy as Volatility Regime Detector

### Source Papers
- **Primary**: Singha (2025). "Hidden Order in Trades Predicts the Size of Price Moves." arXiv:2512.15720v1. [q-fin.TR, q-fin.ST]
- **Supporting**: Muhle-Karbe, Ouazzani Chahdi, Rosenbaum, Szymanski (2026). "A unified theory of order flow, market impact, and volatility." arXiv:2601.23172v2. [q-fin.ST, q-fin.TR]

### Core Idea
Singha demonstrates that **order-flow entropy** (computed from a 15-state Markov transition matrix at second resolution) predicts the **magnitude** of subsequent price moves without providing directional information. On 38.5M SPY trades over 36 days:
- Low entropy (< 5th percentile) amplifies subsequent 5-minute absolute returns by **2.89x** (t=12.41, p<0.0001)
- Directional accuracy remains at 45% (indistinguishable from chance)
- Walk-forward validation across 5 non-overlapping periods confirms OOS predictability
- Label-permutation placebo test: z=14.4 against null

The key insight: entropy is **sign-invariant** -- it detects the PRESENCE of informed trading without revealing its direction. Low entropy = ordered flow = large move coming. High entropy = noise = small move.

Muhle-Karbe et al. provide the theoretical foundation: order flow decomposes into "core" (informed) and "reaction" (noise) components. The persistence of core flow (measured by H0~3/4) determines volatility roughness, impact power law, and volume dynamics simultaneously.

### Why It Might Work for TMFD6
1. **Predicts magnitude, not direction**: Avoids the proven-dead directional prediction problem entirely. Instead, it tells us WHEN large moves will occur.
2. **Actionable for execution timing**: If we know a large move is imminent (low entropy), we can defer trading to avoid adverse fills. If entropy is high (noise regime), we can execute safely via passive limits.
3. **Actionable for volatility trading**: Low entropy -> high expected vol -> widen our execution tolerance or increase position sizing for mean-reversion.
4. **Composable with spread regime**: Our strongest finding is the 10-20x regime effect. Entropy could be the mechanism BEHIND spread regime changes -- wide spreads may coincide with low entropy (informed flow).
5. **Computationally simple**: 15-state Markov matrix at 1-second resolution. No deep learning needed. Our FeatureEngine v2 can compute this.
6. **58 days of data is sufficient**: Singha validated on 36 days. We have 58.

### Key Risks
- **Requires trade classification**: The 15-state Markov matrix needs trade-level data (buy/sell classification). Our TMFD6 data is L5 snapshots, not individual trades. May need to infer trade direction from quote changes (Lee-Ready or similar).
- **SPY is not TMFD6**: SPY has 100x+ the liquidity. Entropy patterns may differ on a thin Mini-TAIEX contract.
- **36-day validation is thin**: The author acknowledges "limited sample requires extended validation."
- **Not directly tradable**: Predicting magnitude without direction is useful for risk/timing but not for directional alpha. Needs combination with another signal.

### Data Requirements
- TMFD6 tick data with inferred trade direction (PARTIAL: can infer from bid/ask changes)
- 1-second Markov transition matrix computation (BUILD)
- Forward absolute return at 1-min/5-min horizons for validation

### Estimated Signal Half-Life
Minutes (entropy regimes persist for minutes to hours)

### Quick Validation
Classify each TMFD6 quote change as buyer/seller-initiated (tick rule or Lee-Ready). Compute 30-second rolling entropy. Split into quintiles. Measure mean absolute 5-minute forward return per quintile. If Q1/Q5 ratio > 2, signal is live.

---

## Candidate #2: Push-Response Conditional Mean-Reversion

### Source Papers
- **Primary**: Vlasiuk & Smirnov (2025). "Push-response anomalies in high-frequency S&P 500 price series." arXiv:2511.06177v1. [q-fin.TR, q-fin.CP, q-fin.ST]
- **Supporting**: Safari & Schmidhuber (2025). "Trends and Reversion in Financial Markets on Time Scales from Minutes to Decades." arXiv:2501.16772v2. [q-fin.ST, q-fin.TR]

### Core Idea
Vlasiuk & Smirnov form ordered pairs of backward price increment ("push") and forward price increment ("response") across 1,500 SPY trading days. Key findings:
1. For short lags (1-5,000 ticks): responses cluster near zero (efficient)
2. Beyond ~5,000 ticks: **large pushes produce predictable non-zero responses**
3. **Asymmetric**: large negative pushes produce STRONGER positive responses than equally large positive pushes (consistent with asymmetric liquidity replenishment after sell-side shocks)
4. The anomaly is "invisible in unconditional returns" -- only visible in conditional analysis
5. Decomposition into symmetric (mean-reversion) and antisymmetric (momentum) components shows that mean-reversion dominates at medium lags

Safari & Schmidhuber extend this across 330 years of data and multiple asset classes: **markets are in a trending regime on timescales from hours to years, and in a reversion regime on shorter and longer timescales.** The transition point is asset- and period-specific.

### Why It Might Work for TMFD6
1. **Conditional, not unconditional**: We proved that unconditional directional prediction is dead on TMFD6. Push-response is fundamentally different -- it conditions on a large preceding move, which changes the distribution.
2. **Asymmetric reversion**: Sell-side shocks revert more strongly. This is structural (limit orders replenish the bid side after large sells) and should transfer to TMFD6.
3. **TMFD6 is LESS efficient than SPY**: If the anomaly exists on SPY (the most efficient market), it likely exists on TMFD6 with larger magnitude.
4. **Compatible with our wide-spread finding**: Large pushes often coincide with wide spreads. In the Jan/Feb regime where our execution timing signal was strongest (IC=0.19), push-response would add actionable directionality.
5. **Strategy**: After a 2+ sigma price drop within 30 seconds, post a passive buy at the bid. Wait for reversion. Exit on mean reversion or timeout. One-way trade (no round-trip cost until we decide to exit).
6. **~5,000 tick lag on SPY ≈ several minutes on TMFD6**: SPY has ~10,000 ticks/minute. TMFD6 has ~108 ticks/minute. So 5,000 SPY ticks ≈ 50 TMFD6 ticks ≈ 30 seconds. This is within our viable holding period.

### Key Risks
- **SPY at 5,000 ticks is very different from TMFD6 at 50 ticks**: The structural lags may not scale linearly.
- **"Tradable pockets" may be too rare**: Large pushes (2+ sigma) may occur only a few times per day on TMFD6.
- **Reversion magnitude may be < 4 pts cost**: Even if reversion is predictable, it must exceed RT cost.
- **Exit timing**: Mean-reversion strategies need a clear exit signal. Holding too long incurs random walk risk (MAE grows as sqrt(time)).

### Data Requirements
- TMFD6 tick-by-tick mid-price series (HAVE)
- Push magnitude computation at various time horizons (BUILD)
- Conditional response estimation (statistical, not ML)

### Estimated Signal Half-Life
30 seconds to 5 minutes (dependent on push size; larger pushes = longer reversion)

### Quick Validation
On TMFD6 data: (1) compute 30-second returns, (2) identify tails (> 2 sigma), (3) measure mean forward 60s/120s/300s return conditioned on tail direction. If mean response is > 2 pts (half of RT cost) and asymmetric (negative pushes revert more), the signal is live.

---

## Candidate #3: Closing Auction Market-Making

### Source Papers
- **Primary**: Graf & Mastrolia (2026). "Learning Market Making with Closing Auctions." arXiv:2601.17247v1. [q-fin.TR]
- **Supporting**: Kang (2025). "Sources and Nonlinearity of High Volume Return Premium." arXiv:2512.14134v2. [q-fin.TR]

### Core Idea
Graf & Mastrolia develop a Deep Q-Learning market-making framework that explicitly incorporates the closing auction mechanism. Instead of using terminal inventory penalties (the standard approach which is disconnected from reality), they continuously refine the projected clearing price and make decisions that anticipate the auction.

Key insight: the closing auction is a massive liquidity event where institutional investors must execute. The continuous session approaching the auction has predictable dynamics (increased volume, spread compression, price convergence to anticipated clearing price).

Kang (2025) on Korean market data (structurally similar to TWSE) shows that **institutional buying intensity normalized by market cap** produces monotonic return predictions. The highest-conviction institutional buying generates large cumulative abnormal returns over 50 days.

### Why It Might Work for TMFD6
1. **TAIFEX has closing auctions**: TMFD6 has a defined closing call auction. The dynamics around session boundaries should be exploitable.
2. **Korean market analogy**: Kang's Korean market findings are directly relevant to TWSE/TAIFEX (similar market structure, similar institutional mix, similar trading hours).
3. **Not directional prediction**: The strategy is about capturing the spread compression and price convergence near auction time, not predicting direction.
4. **Predictable volume pattern**: Volume increases toward session end. This is a known calendar effect that provides the foundation for the strategy.
5. **Retail advantage**: Large institutional investors MUST trade at the close (benchmark requirements). Retail traders have the flexibility to front-run this predictable demand.
6. **Session boundary effects on TMFD6**: Our data spans regular sessions. We can analyze price behavior in the final 15 minutes before close.

### Key Risks
- **TMFD6 closing auction may be thin**: Mini-TAIEX may have minimal closing auction participation compared to full-size TX or stock markets.
- **Deep Q-Learning implementation**: The paper uses RL, which requires significant infrastructure. However, the INSIGHT (anticipated clearing price convergence) could be implemented without RL.
- **Competing with algos**: Session-boundary effects are well-known. Institutional algos already optimize for close. A retail trader at 36ms may be too slow.
- **Limited signal**: The closing auction happens once per day. At best, this provides 1-2 trades per day.

### Data Requirements
- TMFD6 intraday price/volume profile with session timestamps (HAVE)
- Session boundary identification (need to identify regular session hours)
- Volume and spread analysis in final 15/30 minutes of session

### Estimated Signal Half-Life
15-30 minutes (session-boundary effect)

### Quick Validation
Analyze TMFD6 price and spread behavior in the final 30 minutes of each session across 58 days. Is there systematic spread compression? Does mean-reversion strengthen near close? Is volume predictably higher?

---

## Papers Reviewed but Rejected (Survey V2)

| Paper | arXiv ID | Reason |
|-------|----------|--------|
| Trends and Reversion (Safari & Schmidhuber) | 2501.16772 | Used as SUPPORTING evidence for Candidate #2 but too macro-scale (hours-to-decades) for standalone direction |
| Unified theory of order flow (Muhle-Karbe et al.) | 2601.23172 | Theoretical framework; used as SUPPORTING for Candidate #1 but not directly actionable |
| Strategic Informed Trading (Anthropelos & Robertson) | 2404.08757 | Pure game theory, no empirical validation |
| Deep Hedging with Options (Francois et al.) | 2504.06208 | Hedging framework, not trading strategy; requires options positions we don't hold |
| Volatility Forecasting (various) | Multiple | Volatility prediction alone is not actionable without a volatility trading instrument (we trade futures, not vol) |
| vPIN (Privacy-Preserving NN) | 2411.07468 | Not the financial VPIN -- this is a computer science paper about neural network privacy |
| Market Making with Closing Auctions (full RL approach) | 2601.17247 | RL infrastructure too heavy; used INSIGHT only for Candidate #3 |
| CTA Replication (Benhamou et al.) | 2507.15876 | Monthly/weekly trend-following; wrong timescale for HFT |
| Multimodal Stock Prediction | 2503.08696 | Requires Twitter/news sentiment data we lack for TWSE |

### Note on VPIN
The specific search for "VPIN" and "Volume-synchronized PIN" yielded no relevant financial papers in recent arXiv. The VPIN concept (Easley, Lopez de Prado, O'Hara 2012) is well-established but not the subject of recent novel research. However, the order-flow entropy approach (Candidate #1) is conceptually related and represents the modern evolution of VPIN -- both detect informed trading from order flow patterns. Candidate #1's entropy approach is superior because it uses richer state representation (15-state Markov vs binary buy/sell classification).

### Note on TXO Options Data
The 33M TXO rows in ClickHouse represent a significant untapped data source. Options-informed futures trading (e.g., using put-call ratio, implied volatility skew, or unusual options activity to predict futures moves) is a well-established concept but was NOT the focus of recent arXiv papers. This direction should be explored as an ENGINEERING task (data analysis) rather than a literature search. If the team decides to pursue options-based signals, the data exists and the concepts are mature.

---

## Ranking and Recommendation

### 1. Candidate #1: Order-Flow Entropy (HIGHEST CONVICTION)
- **Why**: Predicts MAGNITUDE not direction. Directly addresses the "dead directional signal" problem. Computationally simple. Strong empirical backing (z=14.4 against null). Composable with our spread regime finding (entropy may explain WHY spreads widen/narrow).
- **Risk level**: MEDIUM. Requires trade direction inference from L5 snapshots. May not transfer from SPY to TMFD6.
- **Quick validation**: 2-3 hours of analysis work.

### 2. Candidate #2: Push-Response Mean-Reversion (HIGH CONVICTION)
- **Why**: Novel direction not explored in any prior round. Conditional on large moves (not unconditional prediction). Asymmetric (sell shocks revert more). Literature shows it exists on SPY across 1,500 days.
- **Risk level**: MEDIUM. "Tradable pockets" may be too rare on TMFD6. Reversion magnitude unknown.
- **Quick validation**: 1-2 hours. Straightforward conditional return analysis.

### 3. Candidate #3: Closing Auction Effects (SPECULATIVE)
- **Why**: Unexplored direction. Structural (institutional must-trade) basis. Unique to session-based markets. Korean market analogy supports it.
- **Risk level**: HIGH. May be competed away. Only 1-2 trades per day. TMFD6 auction may be too thin.
- **Quick validation**: 1 hour. Analyze last 30 minutes of each TMFD6 session.

### Recommended Validation Sequence
1. **Push-response map** on TMFD6 (Candidate #2) -- fastest to validate, most novel
2. **Order-flow entropy quintile analysis** (Candidate #1) -- requires trade direction inference first
3. **Session-end pattern analysis** (Candidate #3) -- simple descriptive statistics
4. If entropy works, combine with spread regime for actionable timing strategy
