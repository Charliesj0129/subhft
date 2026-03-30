# R23 Stage 1: Strategic Viability Survey

**Date**: 2026-03-28
**Researcher**: Claude (R23 Researcher Agent)
**Scope**: Can this platform generate alpha given its constraints?

## Executive Summary

After 22 rounds of alpha research and an exhaustive arXiv survey across 7 search directions (~80 papers reviewed), I identify **3 candidate directions** with feasibility >= MEDIUM and provide an honest assessment of the platform's strategic position.

**Bottom line**: The platform has a NARROW but REAL viable path. It requires abandoning the HFT identity and pivoting to medium-frequency (5min-4hr) regime-conditional strategies. The existing infrastructure (FeatureEngine, LOB processing, ClickHouse) becomes a competitive advantage for signal generation, even though execution cannot compete at microsecond timescales.

---

## Prior Constraint Summary (from R1-R22)

| Constraint | Value | Implication |
|---|---|---|
| Broker RTT (P95) | 36ms submit, 43ms modify, 47ms cancel | Cannot compete for queue priority |
| TMFD6 RT cost | 3.92 pts (1.19 bps) | Kills signals with edge < 2 bps |
| TXFD6 RT cost | ~2.18 bps | Even worse cost/signal ratio |
| Median tick interval | 125ms (TXFD6) | Low-frequency market by global standards |
| Maker rebates | NONE | Pure cost, no rebate offset |
| Sell tax | 2.0 bps (full) | Asymmetric cost penalizes round-trips |
| L1 microstructure | EXHAUSTED (R14-R22) | IC too weak, signal-horizon mismatch |
| MM strategies | DEAD (R13, R16) | Adverse selection at 36ms RTT |
| L5 depth | NON-INFORMATIVE | L2-L5 adds noise on Taiwan futures |
| HF->MF extension | IMPOSSIBLE (R19) | OFI decays as OU, tau~15s |

## Directions Surveyed

### Direction 1: Medium-Frequency Trend/Reversion (HOURS scale)
**Papers**: Safari & Schmidhuber 2025 (2501.16772), Schmidhuber 2020 (2006.07847), Wood et al. 2026 "DeePM" (2601.05975), Rosenzweig 2026 (2601.11201)
**Finding**: Strong evidence that futures markets exhibit a universal trending regime on timescales from hours to years, with mean-reversion on shorter scales. Safari & Schmidhuber (2025) extend tick-level analysis across 14 years of futures data and find the transition from reversion to trending occurs at ~1-4 hours. DeePM (Wood et al. 2026) demonstrates a deep learning trend-following strategy on 50 diversified futures with realistic transaction costs achieving 2x the Sharpe of classical CTA strategies. Singha et al. (2511.08571) show trend-momentum on gold futures with Sharpe 2.88 net of 0.7 bps cost.
**Relevance**: HIGH for this platform. The trending regime (hours-scale) is ABOVE the signal-horizon mismatch zone (seconds) and WITHIN the cost-recovery zone.

### Direction 2: Cross-Asset Options-to-Futures Lead-Lag
**Papers**: Michael et al. 2022 (2201.09319), Zhang et al. 2023 (2305.06704)
**Finding**: Michael et al. show option volume imbalance predicts overnight equity returns, with strongest signals from market-maker volumes and high-IV contracts. Put option information content exceeds calls. Zhang et al. provide robust lead-lag detection methodology for multi-factor models.
**Relevance**: MEDIUM. R17 already tested OIDS on TXO -- found 99.7% quotes (not trades), overnight-only signal. TXO trade data pipeline is a prerequisite. The mechanism is sound (informed trading in options precedes spot/futures moves) but data infrastructure gap is blocking.

### Direction 3: Intraday Momentum with Regime Side-Information
**Papers**: Christensen et al. 2020 (2006.08307), Lis et al. 2026 (2602.18912)
**Finding**: HMM-based intraday momentum with side information (realized volatility ratios, intraday seasonality) eliminates the lag problem of traditional momentum filters. The vrr feature [21] already in our FeatureEngine is exactly such side information. Lis et al. show overreaction signals are most predictable at ~10 minute horizons.
**Relevance**: MEDIUM. Requires longer holding periods than current CBS. The 10-minute horizon finding aligns with our cost structure (need ~60s+ to recoup costs).

### Direction 4: Overnight Return Predictability
**Papers**: Glasserman et al. 2025 (2507.04481), Knuteson 2020 (2010.01727), Michael et al. 2022 (2201.09319)
**Finding**: Glasserman et al. demonstrate that overnight returns are predictable using news features, with systematic patterns of continuation and reversal between intraday and overnight periods. Knuteson documents that nearly all stock market gains have been earned overnight. Michael et al. show option volume imbalance predicts overnight returns.
**Relevance**: LOW-MEDIUM for futures. TAIFEX night session (15:00-05:00) exists but liquidity is thin. The overnight premium is well-documented for equities but less clear for index futures. Would require holding overnight positions -- different risk profile.

### Direction 5: Optimal Passive Execution / Fill Probability
**Papers**: Lokin & Yu 2024 (2403.02572), Fabre & Ragel 2023 (2307.04863), Ma et al. 2025 (2504.00846)
**Finding**: Fill probability models with state-dependent order flows can optimize limit order placement. Fabre & Ragel show that ML-based fill probability estimation combined with optimal distance-of-placement reduces execution costs significantly. Ma et al. explicitly model the interaction between fill probability and order submission latency.
**Relevance**: HIGH as an enabler. R16 already found passive limit orders save 1.2 pts/trade on TMFD6. Systematic fill-probability optimization could save 0.5-1.0 pts/trade additional. This doesn't generate alpha directly but REDUCES the cost bar that all other strategies must clear.

### Direction 6: Structural/Calendar Effects
**Papers**: Limited arXiv coverage for TAIFEX-specific effects.
**Finding**: R14 Gap Fade C1 (+32 bps, N=27) and R17 Thursday Night Short (+467 pts, N=7) are the existing candidates. Academic literature confirms intraday seasonality patterns exist universally but are market-specific. No TAIFEX-specific academic work found on arXiv.
**Relevance**: MEDIUM but data-starved. N=27 and N=7 are insufficient for statistical confidence. Need 6+ months of additional data accumulation. This is a patience game, not a research gap.

### Direction 7: Alternative Instruments / Markets
**Papers**: No specific papers, but cost structure analysis.
**Finding**: TWSE stock trading has no intraday short-selling for retail. TAIFEX options (TXO) have wider spreads but potential for informed-flow signals. ETFs (0050) have lower transaction costs but tiny tick sizes. The fundamental constraint is TAIFEX's fee structure -- no maker rebates, full sell tax.
**Relevance**: LOW. Switching instruments doesn't solve the structural cost problem. The best path is making existing instruments work with longer horizons and better execution.

---

## Candidate Directions (Feasibility >= MEDIUM)

### Candidate A: Regime-Conditional Intraday Trend Following (HOURS scale)

**Core mechanism**: Detect trending vs. mean-reverting regime using vrr [21] and volatility features, then apply momentum (trending) or contrarian (reverting) strategies with 30min-4hr holding periods. Safari & Schmidhuber (2025) show the trending regime is universal across asset classes on hour-scale timescales.

**Paper references**:
- Safari & Schmidhuber 2025 (arXiv:2501.16772) -- Trend/reversion regimes from minutes to decades
- Schmidhuber 2020 (arXiv:2006.07847) -- Critical phenomena in financial markets, universal scaling
- Wood et al. 2026 (arXiv:2601.05975) -- DeePM: regime-robust deep learning for macro portfolios

**Latency requirement**: RELAXED. Holding period 30min-4hr means 36ms RTT is irrelevant. Entry/exit timing matters but not at microsecond level.

**Cost requirement**: YES. At 30min-4hr holding periods, need only ~2-4 bps edge to be profitable after 1.19 bps RT cost. Trend-following literature consistently shows 5-15 bps/trade edge at these horizons on liquid futures.

**Data requirement**: ALREADY HAVE IT. TMFD6/TXFD6 tick data in ClickHouse. vrr feature [21] already in FeatureEngine. Need to compute multi-horizon trend signals (EMA crossovers, breakout indicators) -- trivial to add.

**Feasibility**: **HIGH**

**Key risk**: TMFD6 spread non-stationarity (Jan/Feb wide vs March tight). Regime detector must be robust to spread regime changes. The DeePM paper addresses this with distributionally robust optimization -- but that requires significant ML infrastructure.

**Why this is different from R14 CBS**: CBS detects a 40bps cascade and trades contrarian with 300s hold. This candidate uses REGIME detection (trending vs reverting) to choose DIRECTION over much longer horizons (30min-4hr). It's fundamentally a different strategy class -- trend following, not mean reversion.

### Candidate B: Execution Cost Reduction via Fill-Probability Optimization

**Core mechanism**: Use LOB microstructure features (already computed by FeatureEngine) to predict fill probability at different price levels, then optimize limit order placement to reduce effective transaction costs by 0.5-1.5 pts/trade. This is not alpha generation -- it's cost reduction that makes OTHER strategies viable.

**Paper references**:
- Lokin & Yu 2024 (arXiv:2403.02572) -- Fill probabilities with state-dependent order flows
- Fabre & Ragel 2023 (arXiv:2307.04863) -- ML for HF execution optimization
- Ma et al. 2025 (arXiv:2504.00846) -- Optimal execution with latency

**Latency requirement**: MODERATE. Benefits from faster execution but works even at 36ms. The key insight from Ma et al. is that latency affects OPTIMAL placement distance -- at higher latency, you should place orders further from mid-price to reduce adverse selection, accepting lower fill rate but better fill quality.

**Cost requirement**: N/A -- this IS cost reduction. R16 showed passive limit orders save 1.2 pts/trade. Systematic optimization could push this to 1.5-2.0 pts/trade, reducing effective TMFD6 RT cost from 3.92 to ~2.0-2.5 pts.

**Data requirement**: ALREADY HAVE IT. LOB features, spread, imbalance, OFI -- all computed. Need historical fill data to train fill-probability model. Can extract from existing ClickHouse order/fill tables.

**Feasibility**: **HIGH**

**Key risk**: Fill rate trade-off. Aggressive cost reduction via passive orders means lower fill rates. Must model the opportunity cost of missed fills. Also, at TMFD6's thin liquidity, large limit orders may not fill at all.

**Why this matters strategically**: Every 1 pt/trade saved in execution cost is equivalent to finding 1 pt/trade of alpha. If effective RT cost drops from 3.92 to 2.5 pts, CBS (which showed +3.00 bps OOS in Jan/Feb) becomes viable even in tighter spread regimes.

### Candidate C: Calendar/Session Pattern Accumulation (DATA COLLECTION)

**Core mechanism**: Accumulate statistical evidence for session-boundary anomalies (opening gap fade, session-end reversion, overnight positioning, day-of-week effects) over 6+ months to reach N>=60 for statistical significance. This is a DATA ACCUMULATION play, not an immediate strategy.

**Paper references**:
- Glasserman et al. 2025 (arXiv:2507.04481) -- Overnight news explains overnight returns
- Knuteson 2020 (arXiv:2010.01727) -- Overnight vs intraday return patterns
- Christensen et al. 2020 (arXiv:2006.08307) -- HMM intraday momentum with seasonality

**Latency requirement**: NONE. These are event-driven signals at session boundaries. Execution has minutes, not milliseconds.

**Cost requirement**: YES. Gap Fade C1 showed +32 bps -- well above 1.19 bps cost. If the pattern holds, cost is not the constraint.

**Data requirement**: TIME. R17 Gap Fade has N=27, needs N>=60 for p<0.05. Thursday Night Short has N=7, needs N>=30. At ~1 event/day for gap fades and ~1/week for day-of-week, need 2-3 months more accumulation.

**Feasibility**: **MEDIUM** (contingent on data accumulation confirming patterns)

**Key risk**: Survivorship/look-ahead bias. The gap fade pattern may be an artifact of the specific 2-month sample. Need strict out-of-sample validation on forward data. Also, session patterns in Taiwan may differ from US/European markets documented in literature.

---

## Directions Explicitly KILLED

| Direction | Why Dead | Evidence |
|---|---|---|
| L1 microstructure alpha | IC too weak, signal-horizon mismatch | R14-R22, 60+ papers |
| Market making | Adverse selection at 36ms | R13, R16 (1080 configs) |
| L5 depth signals | Non-informative on Taiwan futures | R15, R18, R20 |
| HF->MF signal extension | OFI decays as OU, tau~15s | R19 (proven impossible) |
| Cross-asset lead-lag (TSMC) | IC=0.061, p=0.066, marginal | R17 |
| TXO options flow | 99.7% quotes, not trades | R17 (data gap) |
| Alternative instruments | Cost structure is exchange-level, not instrument-level | Structural |

## Strategic Assessment

### The Honest Answer

**Does this platform have a viable path?** YES, but only if it redefines what it is.

The platform was built as an HFT system. Its Rust extensions, ring buffers, and microsecond-optimized pipeline are designed for latency competition. But the market reality is:

1. **You cannot win the latency game** against institutional co-located players on TAIFEX
2. **You cannot win the spread game** without maker rebates
3. **You cannot win the microstructure game** -- L1 signals are exhausted at your cost level

What you CAN do:

1. **Use the HFT infrastructure as a SIGNAL GENERATION platform** -- your FeatureEngine, LOB processing, and ClickHouse storage give you richer real-time features than any retail trader, even if you can't act on them at HFT speed
2. **Trade at MEDIUM frequency** (30min-4hr) where your features predict regime (trending/reverting) and your execution optimizer minimizes costs
3. **Accumulate calendar/session pattern data** as a side channel that requires no infrastructure changes

The pivot is: **FROM "fast execution of weak signals" TO "rich signals with patient execution"**.

### Recommended Priority

1. **Candidate B (Execution Cost Reduction)** -- Immediate ROI. Reduces the cost bar for ALL future strategies. Can prototype in 1-2 weeks using existing data.
2. **Candidate A (Regime-Conditional Trend Following)** -- Core strategy pivot. Uses existing vrr [21] feature. Needs trend signal computation and regime classifier. 2-4 weeks to prototype.
3. **Candidate C (Calendar Pattern Accumulation)** -- Passive data collection. Set up logging now, analyze in 2-3 months when N is sufficient.

### What Would Change This Assessment

- **Access to TXO trade ticks** (not quotes): Would unlock options-flow lead-lag (Direction 2)
- **Colocation on TAIFEX**: Would re-enable spread capture strategies (unlikely for retail)
- **Maker rebate program**: Would change MM economics fundamentally (TAIFEX policy decision)
- **Multi-market expansion** (e.g., SGX MSCI Taiwan): Would provide cross-exchange arbitrage opportunities

---

## Methodology Note

This survey searched arXiv across 7 directions with ~10 queries, reviewing ~80 paper abstracts and reading key papers in detail. Categories searched: q-fin.TR, q-fin.ST, q-fin.PM, q-fin.MF, q-fin.PR. Date range: 2018-2026. The survey is biased toward published academic work and may miss practitioner strategies not covered in academic literature.
