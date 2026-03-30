# Stage 1 Survey — Round 17

## Search Summary

Searched arXiv (q-fin.TR, q-fin.ST, q-fin.PM, q-fin.GN categories) across 10 query directions, reviewing ~120 papers total. Search terms included:
- "lead lag" futures options index futures
- "mean reversion" intraday futures holding period
- "volatility breakout" opening range breakout futures
- "calendar spread" futures statistical arbitrage
- "momentum" / "trend following" intraday futures transaction costs
- "implied volatility" / "options flow" futures predictive signal
- "regime switching" / "hidden markov" trading futures
- "put call ratio" / "options volume" predict underlying returns
- "network momentum" / "momentum spillover" / "lead lag" commodity futures
- "OFI" / "order flow imbalance" long horizon multi-scale futures
- "TAIEX" / "Taiwan" futures trading strategy

Most arXiv results in q-fin.TR are noise-heavy for our specific constraints. The best candidates emerged from cross-instrument information flow, options-informed signals, and multi-scale OFI dynamics.

---

## Candidate 1: Cross-Contract Calendar Spread Lead-Lag (CSLL)

### Papers
- **Li, Chen & Liu (2025)** — "High-frequency lead-lag relationships in the Chinese stock index futures market: tick-by-tick dynamics of calendar spreads" (arXiv:2501.03171)
- **Cont, Cucuringu & Zhang (2021)** — "Cross-Impact of Order Flow Imbalance in Equity Markets" (arXiv:2112.13213)
- **Hu & Zhang (2025)** — "Stochastic Price Dynamics in Response to Order Flow Imbalance: Evidence from CSI 300 Index Futures" (arXiv:2505.17388)

### Signal
Trade TMFD6 (Mini-TAIEX) based on price discovery lag from TXFD6 (full-size TAIEX futures). The near-month/more-liquid contract leads the less-liquid one. The "lead-lag spread" (price deviation between the two contracts after controlling for fair-value basis) mean-reverts predictably.

Li et al. (2025) demonstrate this explicitly for Chinese index futures calendar spreads (CSI 300 near-month vs. next-month/quarterly), showing the near-month contract leads by one tick, and the lead-lag spread has negative feedback (mean-reversion) that predicts returns of the leading asset. Their backtest shows profitability **after transaction costs**.

For our case: TXFD6 (TAIEX futures, much more liquid, tick rate ~8/sec) should lead TMFD6 (Mini-TAIEX, tick rate ~1.8/sec). When TXFD6 moves and TMFD6 lags, enter TMFD6 in the direction of the TXFD6 move.

### Horizon
- Signal half-life: 1-30 seconds (cross-contract price discovery lag)
- **Holding period: 5-60 seconds** (wait for TMFD6 to catch up)
- This is shorter than ideal but the key insight is the SIGNAL is observable from TXFD6, not from TMFD6's own microstructure (which is dead per R16).

### Cost Survival
- Need TMFD6 to catch up by > 4 pts after TXFD6 signals
- TXFD6 1 pt = 200 NTD, TMFD6 1 pt = 10 NTD. A 20-pt TXFD6 move = 1-pt TMFD6 move expectation
- Profitable only on **large** TXFD6 moves (>40-80 pts) that create meaningful TMFD6 lag
- Li et al. show profitability in Chinese market where near-month leads deferred by 1 tick — but Chinese CSI 300 futures have much tighter spreads relative to tick size

### Latency Survival
- Signal comes from TXFD6, which we already subscribe to
- 36ms RTT is acceptable IF the lead-lag window is >100ms (which it should be for TMFD6 at 1.8 ticks/sec = ~555ms between ticks)
- We need to place the order on TMFD6 before the next TMFD6 tick arrives — 555ms window vs. 36ms RTT is comfortable

### Data Needs
- TXFD6 tick+bidask data (need to subscribe, currently have TMFD6 only)
- Fair-value basis model between TXFD6 and TMFD6 (simple: TMFD6 = TXFD6/4 + basis)
- **Gap**: We do NOT currently have TXFD6 data in ClickHouse. Need to add subscription.

### Risks
- **TMFD6 may not lag TXFD6 significantly** — they track the same index, market makers may keep them in sync
- **Spread = 4 pts on TMFD6** means we need >4 pts of catch-up per trade — may only trigger on large moves
- **Low frequency of actionable signals** — possibly 5-20 per day
- **Basis risk** — the two contracts are not perfectly fungible (different multipliers, possibly different expiry months)
- **This is closer to stat-arb than alpha** — requires simultaneous TXFD6 monitoring, more infrastructure complexity

---

## Candidate 2: Options-Informed Directional Signal (OIDS)

### Papers
- **Michael, Cucuringu & Howison (2022)** — "Option Volume Imbalance as a predictor for equity market returns" (arXiv:2201.09319)
- **Kanniainen & Magris (2018)** — "Option market (in)efficiency and implied volatility dynamics after return jumps" (arXiv:1810.12200)
- **Kearney, Shang & Sheenan (2019)** — "Implied volatility surface predictability: the case of commodity markets" (arXiv:1909.11009)

### Signal
Use TXO (Taiwan index options) volume imbalance and implied volatility dynamics to predict TMFD6 direction over 5-60 minute horizons.

Michael et al. (2022) show that the **normalized imbalance between positive-view and negative-view option volumes** strongly predicts **overnight excess returns** of the underlying. Key findings:
1. Market-maker volumes are the strongest signal source
2. **High-implied-volatility options** carry the most predictive information
3. **Put volumes** are more informative than call volumes
4. Nonlinear analysis reveals strong directional predictability

Kanniainen & Magris (2018) show that after return jumps, implied volatility adjusts **gradually** (not instantly), especially for puts and ATM options — creating a window of predictability.

For our case: TXO options trade on the same TAIEX index. We have **33M rows of TXO data untapped**. Options flow (especially put/call volume ratio, IV skew changes, large-lot directional bets) should lead futures by minutes to hours because:
- Informed traders prefer options for leverage
- Options market is less liquid = slower information absorption
- IV changes propagate to futures direction with delay

### Horizon
- **5-60 minutes** for intraday signals
- **Overnight** for end-of-day positioning (per Michael et al.)
- This is well beyond our 36ms RTT concern

### Cost Survival
- 4 pts RT cost = 1.33 bps. Over 30-60 minute horizons, TMFD6 moves average 20-80 pts
- Need only ~5% directional accuracy improvement over random to be profitable
- Options-derived signals are fundamentally different from L1 microstructure (R16 dead zone) — they measure **informed flow** at a macro level
- Michael et al. demonstrate profitability even with transaction costs in equity markets

### Latency Survival
- Signal updates on option trade frequency (seconds to minutes), not tick-level
- 36ms RTT is entirely irrelevant for 5-60 minute holding periods
- Even a 1-second signal computation delay is negligible

### Data Needs
- **TXO options tick data** — we have 33M rows already in ClickHouse (untapped per R16 notes)
- Need: strike, expiry, put/call, volume, price (to derive IV) per tick
- Need to build: IV calculation pipeline, put/call volume ratio, volume imbalance metric
- **No new data subscription needed** — we already have the data

### Risks
- **TXO data pipeline not yet built** — significant development effort to process 33M option rows
- **IV calculation requires Black-Scholes or binomial** — cold-path compute, not hot-path concern
- **Signal may not transfer from US equity (Michael et al.) to Taiwan futures** — different market microstructure
- **Options liquidity on TAIEX may be concentrated in few strikes** — limiting the signal surface
- **Overnight holding** exposes to gap risk (if using the strongest signal from Michael et al.)
- **Most promising but most uncertain** — no prior evidence on TAIFEX specifically

---

## Candidate 3: Futures Curve Slope Momentum (FCSM)

### Papers
- **Bianchi, Fan, Miffre & Zhang (2023)** — "Exploiting the dynamics of commodity futures curves" (arXiv:2308.00383)
- **Li & Ferreira (2025)** — "Follow the Leader: Enhancing Systematic Trend-Following Using Network Momentum" (arXiv:2501.07135)
- **Pu, Roberts, Dong & Zohren (2023)** — "Network Momentum across Asset Classes" (arXiv:2308.11294)

### Signal
Model the term structure (slope) of TAIEX futures across expiry months and trade TMFD6 based on slope changes and cross-contract momentum spillover.

Bianchi et al. (2023) apply Nelson-Siegel framework to commodity futures term structure and find that **slope change** generates significant profits that survive transaction costs. The slope captures contango/backwardation dynamics and their continuation.

Li & Ferreira (2025) and Pu et al. (2023) show that momentum "spills over" across related assets through lead-lag networks, with the network momentum strategy achieving **Sharpe 1.5 and 22% annual return** across 64 futures contracts.

For our case: TAIEX has multiple futures contracts (TX near, TX next, MTX near, MTX next). The term structure slope (near-month vs. next-month spread) changes in predictable ways around roll dates, settlement dates, and index rebalancing events. Additionally, momentum in the large TXFD6 contract spills over to TMFD6 with a lag.

### Horizon
- **Hours to days** for term structure slope signals
- **Minutes** for momentum spillover signals
- Both well beyond 36ms RTT concern

### Cost Survival
- Multi-hour holding periods mean target moves of 50-200 pts on TMFD6
- 4 pts RT cost is 2-8% of expected move — easily survivable
- Bianchi et al. show profitability net of transaction costs for commodity futures

### Latency Survival
- Signal updates on minute/hourly basis — 36ms RTT irrelevant
- Even with aggressive re-entry, the signal is so slow that latency doesn't matter

### Data Needs
- TXFD6 + TMFD6 tick data across multiple expiry months
- **Gap**: Need to subscribe to non-front-month contracts (next-month TX, next-month MTX)
- Settlement date calendar for TAIFEX
- Historical term structure data (can reconstruct from ClickHouse if we have multi-expiry data)

### Risks
- **TAIEX futures term structure may be too simple** — only 2-3 active maturities vs. commodity markets with 12+ months
- **Mini contract tracks full contract nearly perfectly** — slope signal may be identical across both
- **Roll-date effects are well-known** — may already be priced in by institutional traders
- **Network momentum requires broad asset universe** — with only TX/MTX we have a very small network
- **Lowest conviction** of the three candidates due to limited term structure complexity on TAIFEX
- **Signal frequency very low** — possibly 1-3 trades per day or fewer

---

## Rejected During Survey

| Direction | Reason for Rejection |
|---|---|
| L1 microstructure alpha on TMFD6 | Dead zone per R16 — exhaustively tested, signal-horizon mismatch at 4pt cost |
| Bidirectional MM | Dead per R13 — queue-back adverse selection at 36ms RTT |
| LOB kinetic energy / depth features | Dead per R15 — IC too weak |
| VPIN overlay | Dead per R12 — DD -30.6% |
| VIX futures mean-reversion (Li 2016) | Requires VIX futures — different instrument, not applicable |
| Regime-switching HMM general (various) | Too generic — no specific signal for TMFD6 cost model |
| Chinese HF futures return prediction (Peng et al. 2025) | ML-based (SVM/LR), requires LOB data we already showed is insufficient at R16 cost |
| DRL intraday trading (Goluza et al. 2024) | Black-box ML, no cost-awareness for our specific 4pt structure |
| Deep learning stat-arb (Guijarro-Ordonez et al. 2021) | Equity-focused, requires large cross-section universe |
| Crude oil cross-market HMM (Fanelli et al. 2023) | Wrong asset class, different market structure |
| Dynamic grid trading (Chen et al. 2025) | Crypto-focused, grid trading has zero expected value under fair assumptions |
| Technical indicators (Chen et al. 2017) | Showed all profitability vanishes with transaction costs on CSI 300 futures |
| S&P 500 intraday ensemble strategy (Baldovin et al. 2012) | Old, S&P-specific, requires intraday correlations that may not exist on TAIEX |

---

## Recommendation

**Priority 1: Options-Informed Directional Signal (OIDS)** — Highest potential.
- Strongest theoretical backing: option volume imbalance predicts underlying returns (Michael et al. 2022)
- We already have 33M TXO rows untapped — no new data subscription needed
- Signal horizon (5-60 min) completely avoids both the 36ms latency wall and the R16 microstructure dead zone
- Fundamentally different signal source (informed flow via options) vs. everything tried in R12-R16 (all LOB/microstructure)
- Biggest risk is signal transferability from US equity to Taiwan index futures

**Priority 2: Cross-Contract Lead-Lag (CSLL)** — Solid infrastructure play.
- Well-documented mechanism (Li et al. 2025, Cont et al. 2021)
- TXFD6 leads TMFD6 is almost certainly true given 4x liquidity differential
- Latency-safe (555ms inter-tick window on TMFD6)
- But requires new TXFD6 subscription, may trigger infrequently, and profitability per-trade may be marginal at 4pt cost

**Priority 3: Futures Curve Slope Momentum (FCSM)** — Lower conviction, backup.
- Academically sound (Bianchi et al. 2023) but TAIEX term structure is thin
- Very long holding periods help with cost — but signal frequency is very low
- Best as a slow overlay/filter rather than a primary strategy

**Recommended next step**: Prototype OIDS first using existing TXO data. Build a simple put/call volume imbalance indicator and test IC against TMFD6 returns at 5/15/30/60 minute horizons. If IC > 0.02, proceed to strategy design. In parallel, subscribe to TXFD6 data to enable CSLL prototyping.
