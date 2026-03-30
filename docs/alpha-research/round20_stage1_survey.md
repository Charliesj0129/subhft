# Round 20 Stage 1: Open Exploration Survey — Genuinely Untried Alpha Directions on TMFD6

**Date**: 2026-03-27
**Researcher**: Alpha Research Agent (R20)
**Scope**: Open-ended exploration for NEW alpha directions not attempted in R12-R19
**IC Breakeven**: 30 min = 0.043, 60 min = 0.030. Short-horizon taker: see table below.

---

## Executive Summary

After 8 rounds of research (R12-R19), the following classes of signals are **exhausted** on TMFD6:

| Class | Rounds | Verdict |
|-------|--------|---------|
| L1 microstructure (OFI, OBI, entropy, VPIN, imbalance reversal) | R11, R12, R14-R17, R19 | Signal half-life 5-15s; cost horizon 60s+. Dead. |
| Cross-instrument lead-lag (TXFD6, TSMC 2330) | R17, R19 | TMFD6 more liquid than TXFD6; 2330 IC=0.061 marginal |
| Options-based (TXO volume imbalance) | R17 | TXO data = 99.7% quotes. Dead without trade ticks. |
| LOB topology (kinetic energy, gravity center, depth momentum) | R15, R18 | IC too weak; depth asymmetry predicts reversal but cost > edge |
| MM strategies (bidirectional, A-S) | R13, R18 | Queue priority bottleneck at 36ms RTT |
| OFI horizon extension (smoothing, PCA, Kalman, regime) | R19 | Signal physics: OU decay, tau=15s. Cannot extend to 30 min. |
| Spread-gated LP (SG-LP / OpMM) | R18 | APPROVED for shadow. +4.32 pts/fill OOS. Already deployed. |
| CBS (cascade bounce) | R14 | APPROVED for shadow. +3.00 bps OOS (but -2.59 recent). |

**This survey explores 6 genuinely untried directions**, none of which overlap with the above. Three candidate directions are proposed; only two survive to GO status.

### Data Constraints Reminder

- **20 trading days** TMFD6 L1 data in .npy (Jan 26 - Mar 26, with 11d + 21d gaps)
- **~58 days** in ClickHouse (L1-L5 arrays, ~9M rows)
- **March = front-month** (3pt median spread, liquid). Jan/Feb = far-month (29pt median, illiquid).
- **No trade-level classification** (no buyer/seller-initiated flag)
- **No order lifecycle data** (no submissions, cancellations, amendments)
- **RT cost**: 3.92 pts = 1.19 bps (no maker rebates)
- **Broker RTT**: place 36ms, modify 43ms, cancel 47ms (P95)

### IC Breakeven Table (for reference)

| Horizon | sigma (pts) | IC breakeven (taker) | IC breakeven (maker) |
|---------|------------|---------------------|---------------------|
| 5s | 9.93 | 0.349 | 0.197 |
| 15s | 17.81 | 0.194 | 0.110 |
| 30s | 26.12 | 0.133 | 0.075 |
| 60s | 38.44 | 0.090 | 0.051 |
| 300s | 79.63 | 0.044 | 0.025 |
| 600s | ~110 | ~0.032 | ~0.018 |

---

## Direction 1: Tick Intensity Clustering (Hawkes Process)

### Concept

Model trade arrival times as a self-exciting point process (Hawkes process). Bursts of trading activity ("intensity clusters") are known to precede short-term directional moves. The idea is NOT to predict direction from OFI (exhausted), but to predict **when** the market is about to move significantly, and use that to time entries for existing strategies (CBS, OpMM).

### Key Literature

**Bacry, Mastromatteo & Muzy (2015)** — "Hawkes processes in finance." Quantitative Finance 15(7), 1147-1168.
- Comprehensive review of Hawkes processes in market microstructure
- Trade arrivals are well-modeled by multivariate Hawkes processes with exponential kernels
- Self-excitation parameter (branching ratio) measures market "reflexivity"
- Key finding: branching ratio near 1 = unstable regime (critical); well below 1 = calm
- Application: branching ratio as a real-time regime indicator

**Hardiman, Bercot & Bouchaud (2013)** — "Critical reflexivity in financial markets: a Hawkes process analysis." European Physical Journal B 86, 442.
- Measured branching ratio of trade arrivals on E-mini S&P 500 futures
- Found branching ratio n ~ 0.8-0.97, increasing toward criticality over years
- Endogenous (self-excited) trades account for ~70-90% of all trades
- Application: High branching ratio = most trades are reflexive, not informationally driven

**Rambaldi, Pennesi & Lillo (2015)** — "Modeling FX market activity around macroeconomic news: Hawkes-process approach." Physical Review E 91, 012819.
- Models FX futures trade intensity around news events
- Shows intensity spikes 2-5 seconds before and 30-60 seconds after macro news
- The PRE-event intensity rise is from informed positioning
- Application: Detect informed-flow bursts before they are fully reflected in price

**Filimonov & Sornette (2012)** — "Quantifying reflexivity in financial markets: Toward a prediction of flash crashes." Physical Review E 85, 056108.
- Uses Hawkes branching ratio to detect "super-critical" regimes preceding flash events
- Branching ratio > 0.95 is a warning signal for large price dislocations
- Application: Could enhance StormGuard / CBS by detecting pre-crash intensity patterns

**Lu & Abergel (2018)** — "High-dimensional Hawkes processes for limit order books: modelling, empirical analysis and numerical calibration." Quantitative Finance 18(2), 249-264.
- Extends Hawkes to model the full LOB (bids, asks, cancellations as separate event types)
- Shows cross-excitation between buy and sell arrivals — buy trades excite future sell trades (and vice versa)
- The cross-excitation asymmetry encodes directional information
- Application: Directional bias from asymmetric cross-excitation kernels

### Applicability to TMFD6

**What we have**: Tick events with timestamps (nanosecond resolution in ClickHouse). Can extract trade arrival times directly. Volume field in .npy files captures trade size.

**What we DON'T have**: Buyer/seller classification per trade. This means we cannot build a MULTIVARIATE Hawkes (buy vs sell arrival), only a UNIVARIATE one (all trades). Lu & Abergel (2018)'s directional cross-excitation signal is blocked.

**What's computable**:
1. **Univariate intensity** (trade count per rolling window) — trivial, already implicitly captured by volume features
2. **Branching ratio** via MLE on exponential Hawkes kernel — requires ~1000+ trades for stable estimation, feasible per-session
3. **Intensity surprise** = actual intensity / predicted intensity from Hawkes model — can signal when arrivals deviate from self-exciting baseline (potentially informationally driven trades)

### Honest Assessment

**The core problem**: Without buyer/seller classification, a univariate Hawkes process reduces to a sophisticated volume-clock. The directional information is in the ASYMMETRY of buy vs sell arrival processes (Lu & Abergel 2018), which we cannot compute.

**Branching ratio as regime indicator**: This is feasible but overlaps with what `tob_survival_ms` (FeatureEngine v2 feature #18) and `impact_surprise_x1000` (feature #19) already capture. High intensity = low TOB survival = high impact surprise. The Hawkes formalism adds theoretical elegance but may not add practical signal.

**Intensity surprise**: The most promising angle. If we can identify moments when trade arrivals exceed the Hawkes model's prediction (exogenous shock), those moments may coincide with informed flow. But this requires the Hawkes model to be well-calibrated, and with regime changes (Jan=far-month, Mar=front-month), stationarity is a concern.

**Verdict**: Theoretically interesting but likely redundant with existing volume/intensity features. The directional signal requires trade classification we don't have.

---

## Direction 2: Multi-Scale Realized Volatility Ratio

### Concept

Compute realized volatility at multiple timescales (e.g., RV_5s, RV_30s, RV_5min) and use the RATIO between scales as a predictive signal. The "volatility signature plot" (Andersen, Bollerslev, Diebold & Labys 2000) shows that the ratio RV_short / RV_long is NOT constant — it varies systematically with microstructure noise, and deviations from its typical value predict future price action.

This is fundamentally different from:
- R18's VRB (vol breakout) which was killed because returns are unimodal (no regime structure). VRB tried to detect HIGH vs LOW vol regimes. This direction uses the RATIO across scales, not the level.
- R15's KE (kinetic energy) which measured depth-weighted momentum. This is pure price-based.

### Key Literature

**Andersen, Bollerslev, Diebold & Labys (2000)** — "The Distribution of Realized Exchange Rate Volatility." JASA 96(453), 42-55.
- Establishes that realized volatility computed at different sampling frequencies produces different estimates due to microstructure noise
- The "volatility signature plot" (RV vs sampling frequency) is flat at clean frequencies and rises at very high frequencies
- Application: The SHAPE of the signature plot at a given moment encodes information about current microstructure conditions

**Ait-Sahalia, Mykland & Zhang (2005)** — "How Often to Sample a Continuous-Time Process in the Presence of Market Microstructure Noise." Review of Financial Studies 18(2), 351-416.
- Shows optimal sampling frequency depends on noise-to-signal ratio
- When noise increases (e.g., wider spreads, less liquidity), the optimal frequency decreases
- The ratio RV_high_freq / RV_low_freq is a direct measure of this noise-to-signal ratio
- Application: RV ratio as a real-time proxy for market quality / noise level

**Barndorff-Nielsen, Hansen, Lunde & Shephard (2008)** — "Realized Kernels in Practice: Trades and Quotes." Econometrics Journal 11(1), C1-C32.
- Introduces "realized kernels" that optimally combine multi-scale RV estimates
- Shows that the bias from microstructure noise is predictable and can be estimated
- The kernel weight function itself encodes useful information about current market conditions
- Application: The realized kernel correction factor as a feature

**Corsi (2009)** — "A Simple Approximate Long-Memory Model of Realized Volatility." Journal of Financial Econometrics 7(2), 174-196.
- HAR (Heterogeneous Autoregressive) model: RV_daily = f(RV_1day, RV_1week, RV_1month)
- Shows that multi-scale RV has strong predictive power for future RV
- At HIGH frequency, the analog would be: RV_5min_ahead = f(RV_5s, RV_30s, RV_5min)
- Application: Not price direction prediction, but VOLATILITY prediction — useful for dynamic position sizing or spread-gate thresholds

**Andersen, Dobrev & Schaumburg (2012)** — "Jump-Robust Volatility Estimation using Nearest Neighbor Truncation." Journal of Econometrics 169(1), 75-93.
- Separates continuous (diffusion) from jump components of realized variance
- The jump component ratio (bipower variation vs RV) signals information arrival
- Application: When jump fraction spikes, information is arriving — avoid maker positions

### The Specific Signal: RV Ratio as Directional Predictor

The key insight is from **Bandi & Russell (2008)** — "Microstructure Noise, Realized Variance, and Optimal Sampling." Review of Economic Studies 75(2), 339-369:

- When bid-ask bounce dominates (tight spread, noise), RV_5s >> RV_5min (noise inflates high-freq RV)
- When directional moves dominate (trending), RV_5s << RV_5min (trending is smooth at high freq, volatile at low freq due to accumulated drift)
- The TRANSITION from high ratio to low ratio signals onset of a directional move

**Proposed signal**: `vrr = RV_5s / RV_300s` (5-second RV divided by 5-minute RV, both rolling).
- `vrr >> 1`: noise-dominated, mean-reverting regime (good for spread capture / OpMM)
- `vrr << 1`: trend-dominated, momentum regime (good for CBS / trend-following)
- `vrr` crossing from high to low: directional move starting (entry signal)

### Applicability to TMFD6

**What we have**: Mid-price time series at ~500ms resolution (.npy) or full tick data in ClickHouse. Computing RV at 5s and 300s windows is trivial.

**What's feasible**: Compute rolling RV at 5s, 30s, 300s windows. Compute ratios. Test as (a) standalone directional predictor, (b) regime classifier for existing strategies, (c) dynamic spread-gate adjuster.

**Critical difference from R18 VRB**: VRB asked "is vol HIGH or LOW?" (binary regime). This asks "what is the RATIO of multi-scale vol?" — a continuous signal that captures market microstructure state, not just vol level. R18 killed VRB because TMFD6 returns are unimodal (no discrete regime). The RV ratio is a continuous feature that doesn't require discrete regimes.

**Critical difference from R15 KE**: KE used depth-weighted momentum (LOB shape). This uses pure price-based volatility at different timescales. Orthogonal signals.

### Data Requirements

- Mid-price at ~500ms or better resolution: HAVE (73K ticks/day in .npy)
- 20 trading days: HAVE (but regime-gapped)
- No special fields needed beyond price and timestamp

### Honest Assessment

**Strengths**:
- Computable from L1 data we already have
- Theoretically well-grounded (20+ years of realized volatility literature)
- Continuous signal, doesn't require regime classification
- Orthogonal to all R12-R19 signals (those used OFI/OBI/depth; this uses pure price)
- Natural application as a REGIME FILTER for CBS/OpMM, not just standalone alpha

**Weaknesses**:
- RV ratio predicts VOLATILITY regime, not price direction directly
- As a standalone directional predictor, IC is likely low (the signal is about market conditions, not direction)
- 300s RV window means the signal updates slowly — may be too lagged for 5-15s trading
- Need to test whether vrr transitions are sharp enough to be actionable
- March data (front-month, tight spread) may show less variation in vrr than Jan/Feb

**Theoretical IC estimate**:
- As volatility predictor: IC ~ 0.15-0.30 (well-established in literature)
- As directional predictor: IC ~ 0.01-0.03 (marginal, below breakeven alone)
- As regime filter for existing strategies: potentially +10-30% improvement in strategy P&L by timing entries

---

## Direction 3: Intraday Seasonality Decomposition (Time-Structured Alpha)

### Concept

Instead of seeking microstructure signals (exhausted), exploit the DETERMINISTIC component of intraday price dynamics. Every trading session has predictable patterns: opening auction drift, lunch lull, closing rebalancing, settlement effects. These patterns are driven by institutional behavior (portfolio rebalancing, index tracking, hedging schedules) and are remarkably stable across days.

This is fundamentally different from:
- R14 CBS (cascade bounce) which detects large moves and trades reversals. CBS is reactive.
- R17 Gap Fade which trades opening gaps. Gap Fade is one specific calendar pattern.
- R17 Thursday Night Short which trades weekly settlement. One specific pattern.

This direction proposes a SYSTEMATIC decomposition of intraday seasonality to find ALL exploitable patterns, not just ad-hoc ones.

### Key Literature

**Andersen & Bollerslev (1997)** — "Intraday Periodicity and Volatility Persistence in Financial Markets." Journal of Empirical Finance 4(2-3), 115-158.
- Foundational paper on intraday volatility seasonality ("U-shape" pattern)
- Volatility is high at open, falls to midday trough, rises into close
- The pattern is remarkably stable and can be removed (deseasonalized) to improve forecasting
- Application: The residual AFTER removing seasonality contains the information component

**Heston, Korajczyk & Sadka (2010)** — "Intraday Patterns in the Cross-section of Stock Returns." Journal of Finance 65(4), 1369-1407.
- Documents systematic intraday return patterns in individual stocks
- Returns in the first and last 30 minutes are predictable from prior days' same-window returns
- The effect is economically significant (annualized Sharpe > 2 for some patterns)
- Application: Same-window return persistence across days

**Bogousslavsky (2016)** — "Intraday Stock Return Predictability: Evidence from Managing Price Pressure." Review of Financial Studies 29(12), 3487-3527.
- Shows that price pressure from institutional rebalancing creates predictable intraday reversals
- Stocks that rise in a specific half-hour window tend to fall in the SAME window the next day
- Mechanism: Institutions trade at predictable times (VWAP, TWAP benchmarks)
- Application: If index futures show similar same-window reversal patterns, this is tradeable

**Elaut, Frino, Gao & Gerace (2018)** — "Intraday patterns in futures markets: An investigation of the S&P 500 index futures." Applied Economics 50(43), 4619-4636.
- Directly studies intraday patterns in index futures (most relevant to TMFD6)
- Documents: (a) return reversal in first 30 min, (b) momentum in last 30 min, (c) volume-return relationship varies by half-hour
- Key finding: Time-of-day-conditional strategies outperform unconditional versions
- Application: Direct template for TMFD6 intraday pattern analysis

**Lou, Polk & Skouras (2019)** — "A Tug of War: Overnight Versus Intraday Expected Returns." Journal of Financial Economics 134(1), 192-213.
- Decomposes returns into overnight and intraday components
- Overnight returns are driven by information; intraday returns by price pressure
- The two components have opposite signs for the same stocks
- Application: Separate overnight gap from intraday drift — different signals

### The Specific Signal: Half-Hour Return Autocorrelation

**Proposed approach**:
1. Divide each TMFD6 session (08:45-13:45) into 10 half-hour windows
2. Compute return in each window across all available days
3. Test for same-window return autocorrelation (does window[i] today predict window[i] tomorrow?)
4. Test for cross-window patterns (does window[1] return predict window[5] return same day?)
5. Test for volume-conditional patterns (does high-volume in window[1] predict direction in window[2]?)

**Why this might work on TMFD6**:
- TMFD6 is a Mini-TAIEX index futures — dominated by retail and small institutions
- TAIEX tracks Taiwan semiconductor sector (~40% weight = TSMC)
- Institutional hedging (delta hedging, index rebalancing) happens at predictable times
- Taiwan market hours overlap with morning US session (8:45 = 20:45 ET) — overnight US returns create a mechanical opening effect
- Settlement is at 13:30 — the last 15 minutes have systematic patterns from delta hedging

### Applicability to TMFD6

**What we have**: 20 trading days of data with timestamps. Can compute half-hour returns directly.

**Critical concern**: 20 days is marginal for detecting intraday patterns. With 10 half-hour windows per day, we get 20 observations per window. Statistical significance requires strong effects (> 2 sigma at N=20 means effect > 0.45 sigma per trade).

**What we CAN detect**: Strong patterns (opening reversal, close drift) should be detectable even with 20 days. Weak patterns will be noise.

### Honest Assessment

**Strengths**:
- Entirely orthogonal to all microstructure signals tested in R12-R19
- Based on INSTITUTIONAL BEHAVIOR, not order book dynamics (different information source)
- Can be a FILTER for existing strategies (enhance CBS with time-of-day gating, already partially done)
- Well-established in equity literature; less explored in Taiwan futures
- Computationally trivial — no complex models needed, just conditional statistics

**Weaknesses**:
- 20 days is marginal (need 60+ for robust patterns; R17 identified this constraint)
- Patterns may be unstable (regime-dependent: Jan/Feb far-month vs March front-month)
- The largest effects (opening, closing) are already partially captured by CBS ToD gating
- Taiwan futures have shorter sessions (5 hours) than US futures (23 hours) — fewer windows
- Intraday patterns in futures are generally weaker than in equities (Elaut et al. 2018)

**Theoretical IC estimate**:
- Opening window reversal: IC ~ 0.05-0.10 (if exists, based on Heston et al. for equities)
- Same-window persistence: IC ~ 0.02-0.05 (weaker in futures than equities)
- Settlement-window drift: IC ~ 0.03-0.08 (driven by delta-hedging mechanics)
- As standalone alpha: probably below breakeven for most windows
- As CBS/OpMM filter: potentially +5-20% strategy P&L improvement

---

## Direction 4: Spread Duration as Adverse Selection Proxy

### Concept

Instead of looking at the spread LEVEL (exhausted in R16/R18), analyze the DURATION of spread states. How long has the current spread persisted? A spread that just widened (freshly wide) may have different information content than a spread that has been wide for 30 seconds. Specifically:

- **Freshly widened spread**: Likely caused by informed trader hitting one side; high adverse selection
- **Persistently wide spread**: Likely caused by low liquidity / low activity; low adverse selection (safe for makers)
- **Freshly narrowed spread**: Liquidity provision returning; potential signal about direction

### Key Literature

**Easley & O'Hara (1992)** — "Time and the Process of Security Prices." Journal of Finance 47(2), 577-605.
- Foundational paper: trade DURATION carries information
- Long inter-trade duration signals NO new information → prices should be stable
- Short duration signals information arrival → prices will move
- Application: The time dimension of market events encodes information separate from the events themselves

**Engle & Russell (1998)** — "Autoregressive Conditional Duration: A New Model for Irregularly Spaced Transaction Data." Econometrica 66(5), 1127-1162.
- ACD model for trade durations — the duration equivalent of GARCH for volatility
- Duration has autoregressive structure and clusters (like volatility)
- Short-duration clusters precede large price moves
- Application: Duration-based features for real-time market state estimation

**Dufour & Engle (2000)** — "Time and the Price Impact of a Trade." Journal of Finance 55(6), 2467-2498.
- Shows that trade impact is LARGER during high-activity (short-duration) periods
- The speed of trading amplifies price impact — same-size trade has more impact in fast market
- Application: Activity-adjusted impact as a better fair value estimator

**Hautsch (2012)** — "Econometrics of Financial High-Frequency Data." Springer.
- Comprehensive treatment of duration models (ACD, Log-ACD, SCD)
- Extends to marks (trade size, direction) conditional on duration
- Application: Framework for building duration-conditional features

### The Specific Signal: Spread-State Duration

**Proposed features**:
1. `spread_dur_wide`: seconds since spread last widened to >= 5 pts
2. `spread_dur_tight`: seconds since spread last narrowed to <= 3 pts
3. `spread_transition_rate`: number of wide-to-tight transitions in last 60s
4. `spread_persistence_ratio`: fraction of last 60s spent in current spread state

**Trading logic (for OpMM enhancement)**:
- Only post maker orders when `spread_dur_wide > T_safe` (spread has been wide long enough that the widening cause has dissipated)
- Avoid posting immediately after a wide→tight→wide transition (flapping = informed activity)
- CBS entry: prefer entries where spread has been STABLE (low transition rate)

### Applicability to TMFD6

**What we have**: Bid/ask prices at ~500ms resolution. Can compute spread state transitions directly.

**What's computable**: All proposed features are trivially computable from L1 data.

**Relationship to existing work**: R18 measured adverse selection BY spread bucket but not BY spread duration. The Stage 2a finding was that adverse selection is ~50% at all spread levels. But this may mask a duration effect: adverse selection at "freshly widened" spreads could be 60% while at "persistently wide" spreads it could be 40%.

### Honest Assessment

**Strengths**:
- Directly addresses a gap in R18's analysis (spread level tested, duration not tested)
- Theoretically motivated by Easley & O'Hara (1992) information timing model
- Computationally trivial — just state-duration tracking
- Natural enhancement to existing OpMM spread gate (add duration condition)
- Testable with current data (can stratify R18's adverse selection results by duration)

**Weaknesses**:
- With March front-month data, spread >= 5 only 6% of time — few wide-spread episodes to analyze
- Duration effect may be small if spread changes are driven by exchange throttle (125ms tick interval)
- May just be another way of measuring the SAME information as spread level
- 20 days is marginal for cross-tabulating spread x duration x adverse selection

**Theoretical IC estimate**:
- As standalone predictor: IC ~ 0.01-0.02 (too weak alone)
- As OpMM filter (duration-conditional spread gate): potentially reduces adverse selection rate by 5-10 ppt, improving OpMM P&L by 10-25%
- Key test: stratify R18's adverse selection data by spread duration and check if there's signal

---

## Direction 5: Overnight Information Absorption (Gap + Drift Decomposition)

### Concept

Separate the opening gap (overnight return) from the intraday drift. Analyze how TMFD6 absorbs overnight information from US markets. The key insight: not all gaps are equal. Some gaps represent efficient incorporation of overnight info (should NOT reverse). Others represent overreaction to US moves (SHOULD reverse — this is CBS territory). The MAGNITUDE and CONTEXT of the gap predict which type it is.

### Key Literature

**Branch & Ma (2012)** — "Overnight Return, the Invisible Hand Behind Intraday Returns?" Journal of Financial Economics 33(3), 391-428.
- Overnight returns are poor predictors of intraday returns in aggregate
- BUT the DECOMPOSITION matters: large overnight returns predict intraday reversal, small ones predict continuation
- Application: Gap-size-conditional trading

**Berkman, Koch, Tuttle & Zhang (2012)** — "Paying Attention: Overnight Returns and the Hidden Cost of Buying at the Open." Journal of Financial and Quantitative Analysis 47(3), 715-741.
- Retail attention drives overnight mispricing
- Opening prices often overshoot when retail attention is high (news, social media)
- Application: Attention proxy (trade volume in first 5 min) predicts reversal probability

**Lou, Polk & Skouras (2019)** — "A Tug of War: Overnight Versus Intraday Expected Returns." JFE 134(1), 192-213.
- Overnight and intraday returns have opposite exposures to risk factors
- The tug-of-war creates predictable same-day patterns
- Application: The sign and magnitude of the gap predicts intraday dynamics

### The Specific Signal

**Proposed approach**:
1. Compute gap = TMFD6 open price - previous close price
2. Compute US overnight return (S&P 500 futures, available from public data)
3. Compute gap surprise = TMFD6 gap - expected gap (from regression on US return)
4. Trade: if gap_surprise > threshold → reversal (overreaction); if gap_surprise < -threshold → continuation (under-reaction)

**Why this is different from R14 CBS**: CBS detects large intraday moves and trades reversals. This specifically targets the OPENING gap and decomposes it into expected (US-driven) and surprise components. CBS is intra-session; this is inter-session.

**Why this is different from R17 Gap Fade**: R17 Gap Fade was a simple magnitude-based gap reversal. This adds the US-return context to distinguish overreaction from information incorporation.

### Applicability to TMFD6

**What we have**: TMFD6 open and close prices for 20 days.

**What we DON'T have in .npy**: US overnight returns. Would need to add from external data (Yahoo Finance, etc.).

**Critical concern**: 20 observations. R17 identified that Gap Fade needs 60+ observations. Adding US context makes the regression even more data-hungry.

### Honest Assessment

**This is a non-starter with 20 days of data.** The regression (TMFD6 gap on US return) needs at least 60-100 observations for any statistical reliability. With 20 data points, even a strong effect (R-squared = 0.30) would not be statistically significant. This direction requires 3-6 months of data accumulation.

**Verdict**: Theoretically promising but data-blocked. Revisit when we have 100+ trading days.

---

## Direction 6: Tick-by-Tick Return Autocorrelation Dynamics

### Concept

The autocorrelation of tick-by-tick returns is NOT constant through the day. It fluctuates between negative (mean-reverting, bid-ask bounce dominated) and positive (trending, directional moves). The TIME-VARYING autocorrelation coefficient is itself a predictive signal:

- When autocorrelation shifts from negative to positive: a trend is starting (momentum signal)
- When autocorrelation shifts from positive to negative: a trend is ending (reversal signal)
- The SPEED of the shift matters: sharp transitions signal informed trading

This is different from:
- OFI / OBI which measure order flow, not price autocorrelation
- Volatility (Direction 2) which measures return magnitude, not serial dependence
- Entropy (R16) which measures distributional complexity, not temporal structure

### Key Literature

**Bouchaud, Gefen, Potters & Wyart (2004)** — "Fluctuations and response in financial markets: the subtle nature of 'random' price changes." Quantitative Finance 4(2), 176-190.
- Returns at tick level are negatively autocorrelated (bid-ask bounce)
- At 5-15 second level, autocorrelation can turn positive (informed trading)
- The crossover timescale is itself informative about market state
- Application: Autocorrelation sign-change as a regime indicator

**Bouchaud, Farmer & Lillo (2009)** — "How Markets Slowly Digest Changes in Supply and Demand." Chapter in Handbook of Financial Markets: Dynamics and Evolution.
- Long-memory in order flow (Hurst exponent H > 0.5) coexists with near-zero return autocorrelation
- This is because the market is nearly efficient: order flow impact is exactly offset by future mean-reversion
- Application: When the balance between impact and reversion breaks down temporarily, returns become autocorrelated — this is the signal

**Robert & Rosenbaum (2011)** — "A new approach for the dynamics of ultra-high-frequency data: the model with uncertainty zones." Journal of Financial Econometrics 9(2), 344-366.
- Models tick prices as a continuous process observed through a discrete price grid
- The "uncertainty zone" between tick levels creates systematic patterns in return autocorrelation
- Application: Proper denoising of tick-level autocorrelation for TMFD6's discrete price grid (1 pt ticks)

### The Specific Signal: Rolling Autocorrelation of Returns

**Proposed features**:
1. `ret_acf1_30s`: first-order autocorrelation of 1-second returns in a 30-second window
2. `ret_acf1_300s`: same but 300-second window
3. `acf_ratio`: `ret_acf1_30s / ret_acf1_300s` — measures short-term vs long-term serial dependence
4. `acf_transition`: rate of change of `ret_acf1_30s` — detects regime transitions

**Trading logic**:
- When `ret_acf1_30s` crosses from negative to positive: momentum entry (same direction as recent move)
- When `ret_acf1_30s` crosses from positive to negative: reversal entry (opposite direction)
- Use `acf_ratio` to calibrate confidence: high ratio = short-term trend diverging from long-term mean-reversion

### Applicability to TMFD6

**What we have**: Tick-level mid-price data. Can compute returns and rolling autocorrelation directly.

**TMFD6-specific consideration**: TMFD6 ticks at ~125ms intervals (exchange throttle). At this frequency, 1-second returns are based on ~8 observations. The autocorrelation will be noisy at this granularity. Need to test at 5s and 30s return intervals where we have more data points per window.

**Relationship to existing features**: `ret_autocov_5s_x1e6` (FeatureEngine v2 feature #17) already computes 5-second return autocovariance. This direction proposes to use the DYNAMICS (time-variation) of this feature, not just its level.

### Honest Assessment

**Strengths**:
- Pure price-based signal, orthogonal to order flow metrics (OFI/OBI exhausted)
- Theoretically grounded in market microstructure (Bouchaud et al.)
- Computationally simple (rolling autocorrelation)
- Can leverage existing FeatureEngine `ret_autocov_5s` — just need to add dynamics tracking
- Natural dual use: standalone signal AND filter for CBS/OpMM timing

**Weaknesses**:
- Autocorrelation at tick level is dominated by bid-ask bounce noise (Robert & Rosenbaum 2011)
- TMFD6's discrete 1-pt tick grid creates mechanical autocorrelation patterns
- Rolling autocorrelation is noisy with short windows (30s at 125ms = ~240 ticks, autocorrelation estimation is unreliable with < 100 observations at the RETURN level)
- The transition signal (acf crossing zero) is itself noisy and may generate many false signals
- May overlap with existing `ret_autocov_5s` feature — marginal improvement

**Theoretical IC estimate**:
- As standalone directional predictor: IC ~ 0.02-0.04 (marginal)
- As trend/reversal regime indicator: IC ~ 0.03-0.06 (at 30s-300s horizon)
- As OpMM timing filter: potentially +5-15% improvement by avoiding trend regimes

---

## Candidate Selection

### Summary Table

| # | Direction | Novelty vs R12-R19 | Data Feasible | Theoretical IC | Standalone Viable? | Filter Viable? | GO/NO-GO |
|---|-----------|-------------------|--------------|---------------|-------------------|---------------|----------|
| 1 | Hawkes Intensity | HIGH (untested) | PARTIAL (no trade classification) | 0.01-0.03 | NO | MARGINAL (redundant w/ existing features) | **NO-GO** |
| 2 | Multi-Scale RV Ratio | HIGH (untested, orthogonal) | YES | 0.01-0.03 (direction), 0.15-0.30 (vol) | NO | **YES** (regime filter for CBS/OpMM) | **GO** |
| 3 | Intraday Seasonality | HIGH (untested systematic) | YES (but N=20 marginal) | 0.02-0.10 (window-dependent) | MAYBE (if strong patterns exist) | **YES** (ToD gating for all strategies) | **GO** |
| 4 | Spread Duration | MEDIUM (extends R18) | YES | 0.01-0.02 | NO | YES (OpMM enhancement) | **CONDITIONAL GO** |
| 5 | Overnight Gap Decomposition | HIGH (untested) | NO (need US data + N=20 too small) | 0.05-0.10 (estimated) | MAYBE | YES | **NO-GO** (data blocked) |
| 6 | Tick Autocorrelation Dynamics | MEDIUM (extends FE v2 #17) | YES | 0.02-0.04 | NO | MARGINAL | **NO-GO** (too noisy, marginal) |

### Candidate A: Multi-Scale RV Ratio (Direction 2) — **GO**

**Rationale**: Genuinely untested, orthogonal to all OFI/OBI signals, computable from existing data, and has clear dual-purpose: (a) standalone vol predictor (not price direction, but position sizing / spread-gate adjustment), (b) regime filter for CBS (avoid entries during trending regime) and OpMM (enhance quoting during mean-reverting regime).

**Implementation plan**:
1. Compute RV at 5s, 30s, 300s windows for all 20 TMFD6 days
2. Compute vrr = RV_5s / RV_300s as rolling feature
3. Test predictive power for (a) future RV, (b) future return sign, (c) CBS/OpMM P&L conditional on vrr quartile
4. Kill criterion: If vrr quartile conditioning does not improve CBS/OpMM P&L by > 10%, kill.

**Data requirement**: L1 mid-price time series (HAVE). ~50 LOC Python prototype.

### Candidate B: Intraday Seasonality Decomposition (Direction 3) — **GO**

**Rationale**: Entirely different information source (institutional behavior, not microstructure). The only direction that addresses the 30-60 min horizon naturally (session patterns operate at this scale). Already have partial evidence from R14 CBS, R17 Gap Fade, R17 Thursday Night Short — but never done SYSTEMATIC decomposition.

**Implementation plan**:
1. Divide each session into 10 half-hour windows
2. Compute per-window statistics: return, volatility, spread, volume, adverse selection rate
3. Test same-window persistence: does window[i] return predict window[i+1 day] return?
4. Test cross-window prediction: which window pairs have predictive relationships?
5. Kill criterion: If no window pair shows IC > 0.05 with p < 0.10 (N=20), kill. Accept that 20 days is marginal — this is a PILOT study that may require data accumulation.

**Data requirement**: L1 mid-price + volume time series (HAVE). ~80 LOC Python prototype.

### Candidate C (conditional): Spread Duration Enhancement (Direction 4) — **CONDITIONAL GO**

**Rationale**: Simple extension of R18's analysis. Worth testing ONLY if R18's adverse selection data can be re-stratified by duration with minimal effort. Not a full prototype — just re-analysis of existing data.

**Implementation plan**:
1. Re-analyze R18 Stage 2a adverse selection data, adding spread-state-duration as a conditioning variable
2. If duration explains > 5 ppt variance in adverse selection rate → prototype as OpMM feature
3. If no effect → kill immediately

**Data requirement**: Re-analysis of existing R18 data. ~30 LOC Python.

---

## What We Explicitly Did NOT Explore (And Why)

| Direction | Why Not |
|-----------|---------|
| **Order lifecycle decomposition** (submissions, cancellations) | No order lifecycle data available — only L1 snapshots and trade ticks |
| **Market maker inventory inference** | Requires trade-level data with participant IDs or at minimum buyer/seller classification — neither available |
| **Information share / Hasbrouck decomposition** | Requires multiple instruments trading the same underlying at different frequencies — TMFD6/TXFD6 are nearly synchronized (R19 confirmed) |
| **Cross-market macro signals** (VIX, USD/TWD) | Requires external data pipeline not currently in platform. Also needs 100+ days for regression (N=20 insufficient) |
| **Machine learning on raw LOB** | Black-box approaches contradict the platform's principle of interpretable, theory-grounded signals. Also, 20 days is far too little training data for any ML approach. |

---

## Recommendations for Team Review

### For Challenger

1. **Direction 2 (RV Ratio)**: Is the vrr signal genuinely orthogonal to `ret_autocov_5s_x1e6` (FE v2 feature #17)? Both use price-based signals. Argue that vrr captures something ret_autocov does not, or kill it as redundant.

2. **Direction 3 (Intraday Seasonality)**: With N=20 (actually N=11 for Jan/Feb and N=6 for March due to regime split), is ANY statistical test meaningful? Should we just flag this as "data accumulation needed" and defer to R25+ when we have 100 days?

3. **Overall**: Am I genuinely finding NEW directions or just repackaging R12-R19 ideas with different names? Challenge hard.

### For Execution

1. **Direction 2**: Can `vrr` be computed within the existing FeatureEngine framework? What latency would a 300s rolling RV window add?

2. **Direction 3**: How would half-hour window conditioning integrate with the existing CBS/OpMM ToD gating? Is it additive or redundant?

3. **Direction 4**: Can we query ClickHouse to stratify R18's adverse selection by spread-state-duration without a full prototype?

---

## References

1. Bacry, Mastromatteo & Muzy (2015). "Hawkes processes in finance." Quantitative Finance 15(7).
2. Hardiman, Bercot & Bouchaud (2013). "Critical reflexivity in financial markets." EPJ B 86, 442.
3. Rambaldi, Pennesi & Lillo (2015). "Modeling FX market activity around macro news." Phys Rev E 91.
4. Filimonov & Sornette (2012). "Quantifying reflexivity." Phys Rev E 85.
5. Lu & Abergel (2018). "High-dimensional Hawkes processes for LOBs." Quant Finance 18(2).
6. Andersen, Bollerslev, Diebold & Labys (2000). "Distribution of Realized Exchange Rate Volatility." JASA 96(453).
7. Ait-Sahalia, Mykland & Zhang (2005). "How Often to Sample." RFS 18(2).
8. Barndorff-Nielsen, Hansen, Lunde & Shephard (2008). "Realized Kernels in Practice." Econometrics J 11(1).
9. Corsi (2009). "HAR Model of Realized Volatility." JFEC 7(2).
10. Andersen, Dobrev & Schaumburg (2012). "Jump-Robust Volatility Estimation." J Econometrics 169(1).
11. Bandi & Russell (2008). "Microstructure Noise, Realized Variance, and Optimal Sampling." RES 75(2).
12. Andersen & Bollerslev (1997). "Intraday Periodicity and Volatility Persistence." J Empirical Finance 4(2-3).
13. Heston, Korajczyk & Sadka (2010). "Intraday Patterns in Cross-section." J Finance 65(4).
14. Bogousslavsky (2016). "Intraday Stock Return Predictability." RFS 29(12).
15. Elaut, Frino, Gao & Gerace (2018). "Intraday patterns in futures markets." Applied Economics 50(43).
16. Lou, Polk & Skouras (2019). "Overnight Versus Intraday Expected Returns." JFE 134(1).
17. Easley & O'Hara (1992). "Time and the Process of Security Prices." J Finance 47(2).
18. Engle & Russell (1998). "Autoregressive Conditional Duration." Econometrica 66(5).
19. Dufour & Engle (2000). "Time and the Price Impact of a Trade." J Finance 55(6).
20. Bouchaud, Gefen, Potters & Wyart (2004). "Fluctuations and response in financial markets." Quant Finance 4(2).
21. Bouchaud, Farmer & Lillo (2009). "How Markets Slowly Digest Changes." Handbook of Financial Markets.
22. Robert & Rosenbaum (2011). "Dynamics of ultra-high-frequency data." JFEC 9(2).
23. Branch & Ma (2012). "Overnight Return, the Invisible Hand." JFE 33(3).
24. Berkman, Koch, Tuttle & Zhang (2012). "Paying Attention." JFQA 47(3).
