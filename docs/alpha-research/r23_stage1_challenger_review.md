# R23 Stage 1 Challenger Review

**Date**: 2026-03-28
**Reviewer**: Challenger Agent
**Scope**: R23 Stage 1 Strategic Viability Survey — 3 candidates (A: Regime-Conditional Trend Following, B: Fill-Probability Optimization, C: Calendar/Session Patterns)

---

## Overall Verdict: CONDITIONAL APPROVE (A), CONDITIONAL APPROVE (B), REJECT (C)

Five challenges raised. C1 and C2 are BLOCKING for Candidate A. C3 is HIGH for Candidate B. C4 and C5 are structural observations.

---

## C1: Candidate A's Literature Does NOT Support Intraday Trend Following on Single-Contract Index Futures (BLOCKING)

The survey cites three papers to justify regime-conditional intraday trend following with 30min-4hr holds:

1. **Safari & Schmidhuber 2025 (2501.16772)**: This paper analyzes trend/reversion regimes "from minutes to decades" across equities, interest rates, currencies, and commodities using 14 years of tick data + 30 years of daily prices + 330 years of monthly data. The key finding is that the trending regime begins at "a few hours." However:
   - The paper studies cross-asset, cross-horizon regime structure. It does NOT claim that a single retail trader can profitably exploit the hour-scale trending regime on a single contract after transaction costs.
   - The "trending regime" finding is about autocorrelation SIGN, not MAGNITUDE. Positive autocorrelation at 1-4 hours means past returns weakly predict future returns in the same direction. It says nothing about whether the effect size exceeds 1.19 bps RT cost.
   - The paper explicitly notes: "trends tend to revert before they become strong enough to be statistically significant." This is the OPPOSITE of what a trend-following strategy needs.

2. **DeePM (Wood et al. 2026, 2601.05975)**: This paper achieves 2x Sharpe vs classical CTA on 50 diversified futures using daily closing prices, with deep learning, distributionally robust optimization, and macroeconomic graph priors. The survey claims "5-15 bps/trade edge at these horizons on liquid futures." But:
   - DeePM trades a 50-instrument portfolio with cross-asset diversification. The Sharpe improvement comes from portfolio construction and regime-robust optimization across 15 years, NOT from single-contract directional signals.
   - DeePM uses DAILY prices, not intraday. The survey proposes 30min-4hr holds, which is a fundamentally different timescale from the paper's methodology.
   - DeePM's backtests are on CME/ICE/Eurex futures with sub-ms execution. TAIFEX TMFD6 is not in their universe.
   - The "5-15 bps/trade" claim is not sourced from any of the cited papers. It appears to be an unsupported assertion.

3. **Schmidhuber 2020 (2006.07847)**: Earlier version of the Safari & Schmidhuber work. Same findings, same limitations.

**The fundamental problem**: The survey conflates two different things:
- (a) The empirical observation that autocorrelation is positive at 1-4 hour horizons (true, well-documented)
- (b) The claim that this autocorrelation can be exploited profitably on a SINGLE Taiwan mini-futures contract with 36ms RTT and 1.19 bps RT cost (unsupported)

Trend-following CTAs achieve their returns through diversification across 50-100 instruments and monthly rebalancing, not through intraday momentum on a single contract. The per-trade edge on any single instrument is typically 1-3 bps -- barely above our cost.

**Required response**: Provide a specific, data-backed estimate of per-trade edge in bps for intraday trend following on TMFD6 at 1-4 hour horizons, net of 1.19 bps RT cost. The estimate must come from either (a) a paper that tests on similar single-contract index futures or (b) an empirical measurement on your own ClickHouse data. The DeePM portfolio Sharpe is not transferable.

---

## C2: vrr [21] Is Dead Code -- Candidate A Has No Regime Detector (BLOCKING)

The survey states: "vrr feature [21] already in FeatureEngine" and "vrr [21] already in our FeatureEngine is exactly such side information."

**This is factually incorrect.** As confirmed in the prior Execution Review (E2) and the R22 Stage 4 backtest report:

1. **The registry (`registry.py:140-185`) defines exactly 21 features (indices [0]-[20]).** The last registered feature is `deep_depth_momentum_x1000` at index [20]. There is NO `vrr_5_300_x1000` FeatureSpec in the registry.

2. **vrr computation exists in `engine.py` but is NEVER emitted.** The guard at `engine.py:531` prevents vrr from being included in the output tuple because the registry has exactly 21 features.

3. **The R22 Stage 4 report explicitly states**: "Net: 1 FeatureEngine feature (`vrr_5_300_x1000` [21]), 0 strategy signals" -- but the feature was APPROVED for commit, not actually committed to the registry. The Execution reviewer in the prior review cycle flagged this exact issue.

4. **The MEMORY.md itself notes**: "`mlofi_gradient_x1000` was NEVER added to registry -- only `deep_depth_momentum_x1000` [20] exists." The vrr situation is identical.

**Impact**: Candidate A's core mechanism depends on a regime detector built from vrr. Without vrr in the registry, there is no regime signal. The survey's feasibility assessment ("Data requirement: ALREADY HAVE IT") is wrong.

**Required response**: Acknowledge that vrr is not in the registry. Provide a plan to either (a) register vrr as a prerequisite for Candidate A, or (b) propose an alternative regime detection mechanism that does not depend on vrr.

---

## C3: Candidate B's Cost Savings Are Speculative and May Increase Adverse Selection (HIGH)

The survey claims fill-probability optimization can reduce TMFD6 RT cost from 3.92 to ~2.0-2.5 pts, saving 1.5-2.0 pts/trade. Several problems:

1. **The 1.2 pts/trade passive saving (R16) is already the easy win.** The survey proposes an additional 0.5-1.0 pts on top of that. But R16's finding was specifically for limit orders at the best bid/ask. Further optimization means placing orders DEEPER in the book (further from mid), which on TMFD6's thin book means:
   - Fill rates drop dramatically (R13 showed queue-back adverse selection is THE bottleneck)
   - Orders that DO fill are adversely selected (DeLise 2024 negative drift applies)
   - The opportunity cost of missed fills may exceed the savings

2. **The cited papers assume different market structures:**
   - Lokin & Yu (2403.02572) model fill probabilities on FX spot markets with deep, continuous books. TMFD6 has 3-point median spread and thin depth.
   - Fabre & Ragel (2307.04863) use ML on equity LOB -- again, deep books with multiple price levels of liquidity.
   - Ma et al. (2504.00846) explicitly model latency's effect on optimal placement. Their key insight is that at higher latency, you place further from mid. But further from mid on TMFD6 = 2+ ticks away = almost never fills.

3. **The cost reduction arithmetic is questionable.** TMFD6 RT cost = 3.92 pts = tax (6.6 NTD) + commission (26 NTD per RT) + spread crossing (~3 pts). Tax and commission are FIXED regardless of execution optimization. Only the spread-crossing component (~3 pts RT) can be reduced via passive orders. If you already save 1.2 pts passively (R16), the remaining optimization headroom on spread is ~1.8 pts. Claiming 1.5-2.0 pts ADDITIONAL savings implies near-zero spread cost, which requires fills at or inside the spread on EVERY trade. This is unrealistic at 36ms RTT.

4. **No kill conditions specified.** Unlike the other candidates, Candidate B has no detrended IC threshold, no falsifiable hypothesis, and no measurable kill gate. How do we know when to abandon this direction?

**Required response**:
1. Break down the 3.92 pts RT cost into fixed (tax + commission) and variable (spread) components. Show exactly how much of the variable component is addressable.
2. Specify concrete kill conditions (e.g., "if backtested fill rate < X% at improved price, abandon").
3. Acknowledge the adverse selection trade-off: cheaper fills = more adversely selected fills.

---

## C4: Candidate C Is Not a Research Direction -- It Is a Data Collection Task (MEDIUM, non-blocking)

The survey correctly identifies that Gap Fade (N=27) and Thursday Night Short (N=7) need more observations before statistical significance. It proposes "accumulating data over 6+ months."

This is honest, but it is not a "candidate direction" in the same sense as A or B. There is no research question, no methodology to validate, no kill condition to apply. It is "wait and see." Including it alongside research candidates inflates the apparent option space.

**Recommendation**: Remove Candidate C from the research pipeline and instead create a passive data collection task that runs independently. This frees research resources for A and B without losing the data accumulation benefit.

---

## C5: The Strategic Pivot Narrative Obscures a Hard Question (MEDIUM, non-blocking)

The survey's strategic assessment -- "pivot FROM fast execution of weak signals TO rich signals with patient execution" -- is compelling narrative. But it obscures a critical question:

**If the platform becomes a medium-frequency regime-conditional trader with 30min-4hr holds, what infrastructure advantage does it have over a simple Python script with a broker API?**

The platform's competitive moats are:
- Rust ring buffers, lock-free event routing (irrelevant at 30min hold)
- FeatureEngine with 21 real-time features (most are L1 microstructure -- irrelevant for hour-scale trends)
- ClickHouse storage with nanosecond timestamps (useful for data, not for trading)
- Sub-100ns trade classification (irrelevant for hour-scale decisions)

At the proposed holding periods, the relevant signals are EMA crossovers, volatility ratios, and regime classifiers -- all computable with pandas and a cron job. The elaborate HFT infrastructure adds latency, complexity, and operational risk without proportional benefit.

This is NOT a reason to reject the candidates. But it IS a reason to be honest about what the platform's real edge is at MF timescales. If the edge is "better data" (real-time LOB features fed into regime detection), that should be explicitly validated. If the edge is "faster execution within MF" (entering a trend 30 seconds before a cron-based trader), that is a much smaller advantage than the survey implies.

**Required response**: Identify specifically which existing platform capabilities provide measurable advantage at 30min-4hr holding periods over a simpler implementation.

---

## Summary of Challenges

| # | Challenge | Severity | Candidate | Status |
|---|-----------|----------|-----------|--------|
| C1 | Literature doesn't support single-contract intraday trend following | BLOCKING | A | Unresolved |
| C2 | vrr [21] is dead code, no regime detector exists | BLOCKING | A | Unresolved |
| C3 | Cost savings speculative, adverse selection unaddressed | HIGH | B | Unresolved |
| C4 | Candidate C is data collection, not research | MEDIUM | C | Non-blocking |
| C5 | Platform advantage at MF timescales unclear | MEDIUM | A, B | Non-blocking |

---

## Conditions for Approval

### Candidate A: CONDITIONAL APPROVE
Approve if:
1. **C1 resolved**: Researcher provides empirical autocorrelation or IC measurement on TMFD6 ClickHouse data at 1hr, 2hr, 4hr horizons showing detrended IC > 0.02 (or equivalent edge > 2 bps net of cost). Literature-only claims are insufficient.
2. **C2 resolved**: Researcher acknowledges vrr gap and provides a plan to register it, OR proposes an alternative regime detector computable from existing registered features.

### Candidate B: CONDITIONAL APPROVE
Approve if:
3. **C3 resolved**: Researcher provides cost decomposition (fixed vs variable), specifies kill conditions, and acknowledges adverse selection trade-off.

### Candidate C: REJECT
Not a research direction. Convert to passive data collection task. Revisit in 3-6 months when N is sufficient.
