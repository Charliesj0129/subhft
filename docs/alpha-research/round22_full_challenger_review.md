# Round 22: Full Challenger Review

**Date**: 2026-03-28
**Reviewer**: Challenger (Formal Adversarial Review)
**Scope**: All 46 directions across 7 survey files + R22 Stage 1 candidates
**Prior art context**: R11-R21 results, TMFD6/TXFD6 structural constraints

---

## Part 1: Per-Direction Challenge

### Tier 0 Directions

---

#### T0.1: Instantaneous Volatility Invariant (Danyliv 2019)

**Verdict: CONDITIONAL APPROVE**

**Redundancy check**: Genuinely new formula. Not tested in prior rounds. VRR (R20) measures vol ratio but NOT this closed-form estimator. Different mathematical object.

**Challenge 1 — Discrete price degeneracy**: The formula `sigma = spread * sqrt(V_traded/depth) * P(spread/tick)` relies on spread being a continuous-ish variable. On TMFD6, spread takes values {1, 2, 3, ...} points with median = 3. The `P(spread/tick)` factor becomes a lookup into ~3 discrete bins. With spread stuck at 3 points for 70%+ of the session, the formula degenerates to `sigma ~ sqrt(V_traded/depth)`, which is just a volume/depth ratio. This is NOT the rich invariant the paper describes -- it is a trivial ratio.

**Challenge 2 — Calibration regime dependence**: Danyliv (2019) calibrated on FX and US equities where spread spans a wide continuous range. TMFD6 has a structurally different tick regime. The "invariance" property (sigma is universal across assets) may not hold when the spread distribution is nearly degenerate. Must verify that the estimator's cross-sectional stability (the paper's main claim) still holds within a single instrument's time series.

**Kill criteria**: If spread takes value 3 for > 60% of ticks, report effective dimensionality of the formula; if it reduces to < 2 independent factors, KILL. If IC as CBS regime gate < 0.03 at 60s horizon on March data, KILL.

---

#### T0.2: Execution Optimizer (Limit/Market Switching, Albers 2025)

**Verdict: APPROVE**

**Redundancy check**: Genuinely new capability. No prior round addressed execution optimization. This is cost reduction, not alpha generation -- different value chain.

**Challenge 1 — Fill probability transferability**: The R^2 = 0.946 is from BTC perpetual (Binance), an asset with 10-100x the liquidity of TMFD6. TMFD6 best-level depth is often 1-5 lots. The queue model's explanatory power will be much lower when Q_near and Q_opp take values in {0, 1, 2, 3, 4, 5} rather than {50, 100, 200, ...}. Expect R^2 to drop to 0.3-0.6 on TMFD6. The decision rule still works but the confidence intervals on fill probability estimates will be wide.

**Challenge 2 — Adverse selection on contrarian fills**: CBS buys after cascade drops. If the cascade is genuinely informed (not noise), our limit buy fills precisely because the price continues dropping. The paper acknowledges the fill-probability vs. post-fill-return tradeoff. For CBS, adverse fills mean the cascade was not a bounce opportunity but a genuine trend. This is a strategy risk, not an execution risk -- the optimizer cannot fix a bad signal.

**Kill criteria**: Historical analysis on March 2026 CBS signals showing < 40% of entries would qualify for limit orders (spread >= 2 AND favorable imbalance). If < 40%, the optimizer has too little scope to matter. Expected savings < 0.2 pts/trade = not worth the complexity.

---

#### T0.3: HAR-Style Multi-Window Aggregation (Corsi 2009)

**Verdict: CONDITIONAL APPROVE**

**Redundancy check**: PARTIALLY redundant. FeatureEngine v2 already computes EMA-smoothed features (spread_ema_30, imbalance_ema_30 at indices [5]-[15]). The HAR proposal adds 5s and 300s windows. The 300s window is genuinely new. The 5s window may be redundant with raw tick features (which are effectively ~1s resolution given 125ms tick interval with 8 ticks/second).

**Challenge 1 — Dimensionality explosion without feature selection**: 21 features x 3 windows = 63 features + 42 cross-scale = 126 features. With ~7 CBS trades/day and ~220 trading days, we have ~1,540 observations. Fitting any model on 126 features with 1,540 observations is catastrophically overfitting (ratio ~12:1, need at least 10:1 PER feature for reliable OOS). The survey blithely proposes 126 features without addressing statistical power.

**Challenge 2 — Alpha decay at aggregation boundaries**: Paper 4.2 (Emergence of Randomness) explicitly warns that there is a critical aggregation threshold beyond which predictive structure is destroyed. For TMFD6 with 125ms tick interval, the 300s window contains ~2,400 ticks. At this aggregation level, most microstructure signals are pure noise (R19 showed OFI decays as OU with tau ~15s -- at 300s, signal is attenuated by factor e^(-300/15) = e^(-20) ~ 0). The 300s EMA of OFI is measuring *nothing*.

**Kill criteria**: For each of the 21 features, compute IC at 30s and 300s horizons for each of the 3 EMA windows. If > 50% of 300s-window features have IC < 0.01, the slow window is dead weight. Focus on 5s/30s only.

---

### Tier 1 Directions

---

#### T1.1: Trade Classification (EMO Algorithm, Jurkatis 2020)

**Verdict: APPROVE (Infrastructure)**

**Redundancy check**: Genuinely new infrastructure. No prior round implemented trade classification. R12 (VPIN) used BVC which Andersen-Bondarenko (2014) showed produces artifact signals. EMO/Lee-Ready is the proper implementation.

**Challenge 1 — Large-tick accuracy degradation**: The survey acknowledges this but underestimates the severity. TMFD6 with spread = 1 tick means ALL trades are at bid or ask, so the quote rule is trivially accurate. But when spread = 1 tick, the "signed OFI" from classification adds zero incremental information over unsigned depth-change OFI, because every trade at the ask consumes ask depth and every trade at the bid consumes bid depth. The classification is *already embedded in the depth change*. The 2-3x IC improvement cited in the survey (Section 3, structural finding #3) is from equity markets where spread >> tick and many trades happen inside the spread. On TMFD6, expect < 1.5x improvement.

**Challenge 2 — Timestamp synchronization uncertainty**: The survey mentions this in Section 9.1 but does not quantify it. TAIFEX exchange timestamps have millisecond precision for ticks but BidAsk snapshots arrive through Shioaji callbacks that may have variable delay. If a trade and a quote update are <10ms apart, the "concurrent" bid/ask used for classification may be stale. This systematically biases classification toward the tick rule fallback, degrading accuracy. Must measure the timestamp gap distribution between TickEvent and BidAskEvent for the same exchange event.

**Kill criteria**: After implementation, if signed OFI IC < 1.3x unsigned OFI IC at 30s horizon, the classification adds insufficient value. Also: if > 30% of trades fall to tick-rule fallback (midpoint ambiguity), the classifier is not working as intended on TMFD6.

---

#### T1.2: Hawkes Branching Ratio (Hardiman 2013)

**Verdict: CONDITIONAL APPROVE**

**Redundancy check**: Partially overlaps with VRR (R20). Both measure "volatility regime." The branching ratio measures endogeneity (self-excitation fraction), while VRR measures realized variance acceleration. They could be correlated if high endogeneity = high short-term vol. Must verify orthogonality (rho < 0.3).

**Challenge 1 — Estimation window vs. stationarity tradeoff**: 5-minute rolling windows contain ~2,400 ticks on TXFD6, ~1,000 ticks on TMFD6. MLE for Hawkes exponential kernel requires ~500+ events for stable estimates (Bacry et al. 2015, Section 4.2). TMFD6 is borderline. The estimate will have high variance, producing noisy branching ratio that oscillates not because the regime changed but because the estimate is imprecise. This is a well-known problem: Hawkes calibration on short windows is inherently noisy.

**Challenge 2 — Exponential kernel mismatch**: The true excitation kernel on financial data is power-law (Bacry et al. 2015, paper A1 explicitly states this). Fitting an exponential kernel to power-law data produces biased branching ratio estimates that depend on the observation window length. The branching ratio n from an exponential fit on 5-minute windows will systematically differ from a 30-minute fit -- not because the regime changed, but because the model is wrong. This makes "branching ratio regime detection" partially an artifact of estimation bias.

**Kill criteria**: Estimate n on 5-min, 15-min, and 30-min rolling windows. If rank correlation between n_5min and n_30min < 0.5, the estimate is unstable and not a real regime indicator. Also: if std(n) < 0.05, the signal is flat and useless.

---

#### T1.3: Sym/Antisym OFI Decomposition (Elomari-Kessab 2024)

**Verdict: CONDITIONAL APPROVE**

**Redundancy check**: The antisymmetric component `delta_bid - delta_ask` is precisely our existing `depth_imbalance` feature (or equivalently, unsigned OFI). The symmetric component `delta_bid + delta_ask` (net liquidity provision/withdrawal) IS genuinely new -- we do not currently compute this. Half of this proposal is redundant; half is novel.

**Challenge 1 — VAR parameter stability on TMFD6**: The paper reports "extremely stable" VAR parameters on Eurostoxx (3 years, order-by-order data from a major index future). Eurostoxx has 10-100x the depth and tick rate of TMFD6. Parameter stability is a function of sample size and structural stationarity. TMFD6 underwent a spread regime change between January-February (median spread 7 pts) and March (median spread 3 pts) -- documented in the TMFD6 OpMM research. VAR parameters calibrated on Jan-Feb will not be stable on March data. This is a data artifact, not a structural property.

**Challenge 2 — Symmetric mode is spread prediction, not alpha**: The paper states symmetric modes have "high R-squared prediction scores" but these are for predicting spread dynamics, not price direction. Predicting that the spread will tighten is useful for execution timing (T0.2) but not for directional trading (CBS). The antisymmetric mode predicts direction -- but as noted above, this is just OFI under a different name.

**Kill criteria**: Compute IC of symmetric_depth_change for (a) next-30s return and (b) next-30s spread change. If (a) < 0.02 and (b) > 0.05, confirm it is a spread predictor not a return predictor. If antisymmetric IC < 1.1x existing OFI IC, the decomposition adds nothing.

---

#### T1.4: Trade Sign Autocorrelation (Primicerio 2018)

**Verdict: REJECT**

**Redundancy check**: Requires trade classification (T1.1) as prerequisite, which is itself unvalidated on TAIFEX. Building a dependent signal on unvalidated infrastructure is premature.

**Challenge 1 — TMFD6 tick rate is too slow for autocorrelation structure**: The paper detects "autocorrelation drops" indicating large trader entry on equity markets with ~10ms inter-trade times (tens of thousands of trades per minute). TMFD6 has ~8 ticks/second = ~480 trades/minute. The autocorrelation function of 480 observations per minute is extremely noisy. Detecting a "drop" in autocorrelation requires a baseline of stable autocorrelation, which needs hundreds of lags. With 480 ticks/minute, a 100-lag autocorrelation covers ~12 seconds -- far too short for regime detection.

**Challenge 2 — Redundant with OFI persistence features**: Trade sign autocorrelation measures how persistent order flow direction is. This is functionally identical to `ret_autocov_5s` (FeatureEngine index [17]) and to the OFI OU decay time (R19 finding: tau ~15s). We already have multiple measures of flow persistence. A new one computed from classified trades will be collinear.

**Kill criteria**: N/A -- REJECTED. Do not implement until trade classification is validated AND tick rate concern is addressed with empirical evidence showing autocorrelation structure is detectable at TMFD6's tick rate.

---

#### T1.5: Tick-Rate Volatility Estimator (Lee 2019)

**Verdict: CONDITIONAL APPROVE (but likely redundant)**

**Redundancy check**: HIGH redundancy with VRR (`rv_ratio_x1000`, R22 Stage 1 candidate). VRR = RV_5s / RV_300s. Tick-rate vol = tick_count_30s / tick_count_300s. Since RV ~ tick_count * mean_jump^2, and mean_jump^2 varies slowly, tick_count_ratio and RV_ratio will be highly correlated (the survey itself estimates ~0.7 correlation). This is measuring the same underlying phenomenon (activity acceleration) with a slightly different lens.

**Challenge 1 — Collinearity with VRR**: If rank correlation between tick_rate_ratio and VRR > 0.6, there is no incremental information. Adding a correlated feature to a model with VRR will not improve OOS performance and will increase overfitting risk. Must demonstrate orthogonality first.

**Challenge 2 — Tick count is confounded by quote updates**: Our "tick" stream includes BidAsk updates, not just trades. A burst of quote updates (market makers repositioning) inflates tick count without any trade activity. The Lee (2019) formula assumes ticks = trades. If quote updates are mixed in, the tick-rate vol estimator is measuring quote update rate, which has different information content.

**Kill criteria**: Compute rank correlation with VRR. If > 0.6, KILL. Also: separate trade-tick count from quote-tick count and verify which drives the vol signal.

---

#### T1.6: Cancellation Rate Asymmetry (Anantha 2025)

**Verdict: CONDITIONAL APPROVE**

**Redundancy check**: Genuinely new. We do not currently track depth decrease rate separately for bid vs. ask. This is not the same as depth imbalance (which measures levels, not rates of change).

**Challenge 1 — Snapshot frequency limits rate estimation**: We observe bid/ask snapshots at ~125ms intervals. Depth decreases between snapshots conflate cancellations and executions. A decrease in bid depth could be: (a) a cancellation (informative: market maker withdrawing), or (b) a fill (someone sold at the bid -- also informative but in a different way). Without order-level data (L3/MBO), we cannot distinguish these. The "cancellation rate asymmetry" is actually "depth decrease rate asymmetry" which is a less clean signal.

**Challenge 2 — TMFD6 depth is too thin for meaningful rate asymmetry**: With median depth = 1-3 lots at best bid/ask, depth changes are binary events (lot appears, lot disappears). The "rate" of depth change is dominated by single-lot additions/removals that may be the same market maker repositioning. Cancellation rate asymmetry requires a steady state of depth at each level to measure departures from -- with 1-3 lots, there is no steady state.

**Kill criteria**: Compute the depth-decrease-rate asymmetry (bid_decrease_rate - ask_decrease_rate) over 30s windows. If the standard deviation is > 2x the mean absolute value, the signal is dominated by noise. Also: if depth at best bid/ask is < 3 lots for > 50% of snapshots, the rate estimate is degenerate.

---

#### T1.7: Log-GOFI Stationarization (Su 2021)

**Verdict: APPROVE (minimal cost)**

**Redundancy check**: This is an enhancement to existing OFI, not a new signal. Applying log(1 + |OFI|) * sign(OFI) to our existing OFI features is ~5 LOC and zero-risk.

**Challenge 1 — May not help on TMFD6**: The paper tests on CSI 500 where OFI magnitude varies by 3-4 orders of magnitude intraday. On TMFD6 with 1-3 lots depth, OFI magnitude is bounded in a narrow range. The log transform compresses large values -- if there are no large values, the transform is approximately linear and changes nothing.

**Challenge 2 — Interaction with EMA smoothing**: If we apply log-GOFI before EMA aggregation, the EMA operates on log-compressed values. If we apply after, the stationarization is undone by the EMA. The order of operations matters and the paper does not address this for the multi-scale aggregation case.

**Kill criteria**: Compare IC of log-GOFI vs. raw OFI at 30s and 300s horizons. If difference < 0.005, the transform is not worth maintaining.

---

### Tier 2 Directions

---

#### T2.1: Metaorder Detection (Maitrier/Bouchaud 2025)

**Verdict: REJECT (premature)**

**Challenge 1 — Triple dependency chain**: Requires (1) trade classification (T1.1, unvalidated), then (2) signed trade sequences, then (3) clustering algorithm to detect metaorders. Each step introduces error that compounds. With 85% classification accuracy, 70% sequence accuracy, and 60% clustering accuracy, end-to-end accuracy is ~36%. At that accuracy, detected "metaorders" are mostly noise.

**Challenge 2 — TMFD6 contract size makes metaorders trivial**: TMFD6 is a mini-futures contract (1 point = 10 NTD). Institutional traders use TXFD6 (1 point = 200 NTD) for real positions. TMFD6 is dominated by retail traders who do not split orders into metaorders. The metaorder detection literature is calibrated on major index futures (ES, Eurostoxx) where large institutions systematically split 1000+ lot orders. On TMFD6, a "large order" is 5-10 lots. There may be no metaorders to detect.

**Kill criteria**: N/A -- REJECTED until trade classification is validated AND evidence of order splitting on TMFD6 is established.

---

#### T2.2: LO Arrival/Cancel Rate Asymmetry (Bechler 2017)

**Verdict: REJECT (data limitation)**

**Challenge 1**: Identical to T1.6 cancellation rate asymmetry but requires distinguishing limit order arrivals from cancellations. Without L3/MBO data, we can only infer these from snapshot diffs. Depth increase = arrival, depth decrease = cancel or fill. This is the same signal as T1.6 with the same limitations. **Duplicate of T1.6**.

**Challenge 2**: The paper studies meso-scale LOB dynamics requiring event-level data from Euronext. Our snapshot-based inference introduces timing noise (125ms resolution). The meso-scale dynamics the paper describes (order arrival/cancellation asymmetries over 10-100 event windows) are convolved with our snapshot sampling rate.

---

#### T2.3: Intensity Burst Detection (Christensen 2024)

**Verdict: CONDITIONAL APPROVE**

**Redundancy check**: Partially overlaps with Hawkes branching ratio (T1.2) -- both measure intensity dynamics. However, burst detection is a threshold/event signal, while branching ratio is a continuous regime indicator. Different use cases.

**Challenge 1 — Threshold calibration on TMFD6**: The paper applies to EUR/USD FX with ~100,000 ticks/hour. TMFD6 has ~28,800 ticks/hour (8/sec * 3600). The "heavy traffic" condition required for the non-parametric test may not hold. With lower tick rates, intensity bursts are less frequent and harder to distinguish from normal variation. Must verify the test has sufficient power at TMFD6's tick rate.

**Challenge 2 — Signal actionability**: An intensity burst indicates "something happened." But what action should CBS take? If burst = volatility spike, CBS should AVOID entry (wide spreads, adverse selection). If burst = cascade completion, CBS should ENTER. The burst is ambiguous in direction. Must pair with directional feature to be actionable.

---

#### T2.4: Local Hurst Exponent (Muhle-Karbe 2026)

**Verdict: REJECT**

**Challenge 1 — Requires signed trades**: The paper derives H_0 ~ 3/4 from the persistence of *signed* core order flow. Estimating H_0 from unsigned data is not addressed in the paper. Without trade classification, this reduces to estimating Hurst exponent of price returns, which is a well-studied (and largely useless for short-term prediction) exercise. R-S analysis and DFA on price returns have been extensively tested and found to have IC ~ 0 at 30s-5min horizons on liquid futures.

**Challenge 2 — Estimation window problem**: Hurst exponent estimation requires long series (typically 1000+ observations minimum for DFA). At 30s aggregation, 1000 observations = 8+ hours. This makes the Hurst estimate an all-day average, not a real-time regime indicator. At tick level (125ms), 1000 observations = 125s = 2 minutes, but tick-level Hurst is dominated by microstructure noise (bid-ask bounce).

---

#### T2.5: Spread Widening Duration (Panayi 2014)

**Verdict: REJECT**

**Challenge 1 — TMFD6 spread distribution is too discrete**: Spread takes integer values {1, 2, 3, ...}. "Duration of wide spread" requires defining "wide" relative to a continuous baseline. With 70%+ of time at spread = 3, there is no "widening event" to model -- the spread IS wide by default. Survival analysis requires a meaningful baseline state and an "elevated" state. On TMFD6, these may not be distinguishable.

**Challenge 2 — Collinear with spread_ema features**: We already compute spread_ema at multiple windows (FeatureEngine indices [5]-[15]). A "spread duration" feature is measuring the same information as "how long has spread_ema been above its mean?" This is a reparameterization, not new information.

---

#### T2.6: LOB KE Approximation (Li 2023)

**Verdict: REJECT**

**Redundancy check**: **LOB KE was tested and killed in R15**. The master index acknowledges this ("LOB KE/gravity center: IC too weak, L3-L5 adds noise, R15"). The Tier 2 proposal is a "depth change rate^2" approximation, but this is mathematically equivalent to realized variance of depth changes, which is just depth volatility -- not a new concept.

**Challenge 1**: R15 established that depth asymmetry predicts REVERSAL (not continuation) with IC = -0.025, and L3-L5 depth adds noise. The "approximation" proposed here uses L1-L5, which includes the noisy L3-L5 range. The R15 result should be considered dispositive.

**Challenge 2**: The name "LOB KE" is repackaging depth volatility with physics-sounding terminology. `sum(delta_depth[i]^2)/dt` is literally the realized variance of depth changes.

---

#### T2.7: Event-Driven Aggregation (Elomari-Kessab 2024)

**Verdict: CONDITIONAL APPROVE**

**Redundancy check**: Genuinely different from fixed-window aggregation. Aggregating between "significant price changes" produces variable-length windows that adapt to market activity.

**Challenge 1 — "Significant price change" definition is a hidden parameter**: What constitutes a "significant" move? 1 tick? 2 ticks? 1 bps? On TMFD6 with 3-tick spread, a 1-tick move is the minimum observable change and happens every few seconds. A 2-tick move may happen every 30 seconds. The choice of threshold fundamentally changes the signal's timescale and should be treated as a free parameter that must be optimized -- adding overfitting risk.

**Challenge 2 — Variable-length windows produce non-stationary feature vectors**: Fixed-window features have comparable sample sizes per window. Event-driven windows can range from 2 seconds (volatile) to 5 minutes (calm). The feature distribution (cumulative OFI, tick count) has wildly different scales across window lengths. Normalization by window length partially addresses this but introduces division-by-small-number instabilities for very short windows.

---

#### T2.8: Persistent Depth Change Ratio (Filtration 2025)

**Verdict: CONDITIONAL APPROVE (but may be impractical)**

**Redundancy check**: Novel concept. We do not currently distinguish "persistent" from "fleeting" depth changes.

**Challenge 1 — Definition of "persistent" requires lookback**: To know if a depth change at time t persists, we must wait N snapshots (e.g., 5 snapshots = 625ms). This introduces latency into the feature computation. On the hot path, this means the feature is always 625ms stale. For a signal with half-life < 1s (snapshot features), 625ms staleness may kill the signal entirely.

**Challenge 2 — TMFD6 depth changes are almost always persistent**: With 1-3 lots at each level, a depth change (add or remove 1 lot) is a discrete, meaningful event that persists until the next event. "Flickering" orders (rapid add/cancel cycles) require depth >> 1 lot at each level so that individual order events are small perturbations. On TMFD6, there may be no flickering to filter because every depth change IS the entire depth at that level.

---

### Tier 3 Directions (Brief Assessment)

| Direction | Verdict | Rationale |
|-----------|---------|-----------|
| Path signatures | DEFER | O(d^k * T) computation, offline only. Not actionable for real-time. Research tool, not feature. |
| Wavelet decomposition | REJECT | Theoretically clean but implementation complexity is high for uncertain payoff. HAR-EMAs achieve similar scale separation with O(1) cost. |
| Full PCA mode decomposition | REJECT | Requires order-by-order data (Eurostoxx L3). We have snapshots only. Cannot replicate. |
| Neural HMM regime | REJECT for real-time | Far too heavy for tick loop. The insight (vol modulates aggregation window) is already captured in T0.1 + T0.3 combination. |

### R22 Stage 1 Candidates (Previously Reviewed)

#### rv_ratio_regime (VRR)

**Verdict: CONDITIONAL APPROVE** (unchanged from Stage 1 review)

The R20 diagnostic was never executed. This remains the lowest-risk candidate but has zero empirical evidence on TAIFEX. The discrete mid_price concern (RV_5s degeneracy) is real and must be checked first.

#### imbalance_mr_speed (OU fit)

**Verdict: CONDITIONAL APPROVE** (unchanged from Stage 1 review)

TMFD6 imbalance may be near-binary (thin book). OU fit on a binary process is degenerate. Gate Zero must verify.

#### ofi_run_length (KILLED)

**Verdict: REJECT** (upheld from Stage 1 review). OFI variant in a family killed across R11, R16, R18, R19.

---

## Part 2: Redundancy Map

### Orthogonal Clusters

The 46 proposed directions collapse into **6 truly independent measurement dimensions**:

```
Cluster A: Flow Direction / Imbalance
  ├── Depth imbalance (existing FeatureEngine)
  ├── Antisymmetric OFI (T1.3 antisym component)  ← DUPLICATE of depth imbalance
  ├── Signed OFI (T1.1 + classified trades)         ← INCREMENTAL over depth imbalance
  ├── Trade sign autocorrelation (T1.4)              ← DUPLICATE of OFI persistence
  ├── OFI run length (R22 Stage 1, KILLED)          ← DUPLICATE
  ├── Log-GOFI (T1.7)                               ← TRANSFORM of existing OFI
  └── Multi-level OFI (existing ofi_depth_norm_ppm)  ← ALREADY IMPLEMENTED

Cluster B: Activity Rate / Intensity
  ├── VRR (rv_ratio_x1000, R22 Stage 1)             ← PRIMARY
  ├── Tick-rate vol (T1.5)                           ← DUPLICATE (rho ~0.7 expected)
  ├── Hawkes branching ratio (T1.2)                  ← PARTIALLY INDEPENDENT
  ├── Intensity burst detection (T2.3)               ← EVENT version of same
  ├── Duration CV (tick patterns F2.5)               ← DUPLICATE (inverse of tick rate)
  └── Instantaneous vol (T0.1)                       ← OVERLAPPING with VRR

Cluster C: Depth Dynamics
  ├── Cancellation rate asymmetry (T1.6)             ← PRIMARY
  ├── LO arrival/cancel rate (T2.2)                  ← DUPLICATE of T1.6
  ├── Persistent depth change ratio (T2.8)           ← VARIANT of T1.6
  ├── LOB KE approximation (T2.6)                   ← KILLED in R15
  ├── Symmetric OFI (T1.3 sym component)             ← GENUINELY NEW (net liquidity)
  └── Depth change velocity (bidask P04)             ← VARIANT of T1.6

Cluster D: Temporal Aggregation Method
  ├── HAR 3-window EMA (T0.3)                        ← PRIMARY
  ├── Event-driven aggregation (T2.7)                ← ALTERNATIVE to T0.3
  ├── Volatility-adaptive EMA (cross-freq Method D)  ← ENHANCEMENT of T0.3
  └── Path signatures (Tier 3)                       ← OFFLINE ONLY

Cluster E: Execution Optimization
  ├── Fill probability model (T0.2)                  ← PRIMARY (standalone capability)
  └── Queue-conditioned intensity (tick F2.6)         ← INPUT to T0.2

Cluster F: Regime Indicators
  ├── Imbalance MR speed (R22 Stage 1)               ← PRIMARY
  ├── Spread widening duration (T2.5)                ← DUPLICATE (spread regime)
  ├── Zumbach effect (tick F2.4)                     ← PARTIALLY INDEPENDENT
  └── LOB state Markov transitions (bidask P16-P17)  ← OVERENGINEERED version of regime
```

### Key Redundancy Findings

1. **T1.3 antisymmetric OFI = depth imbalance**. Different name, same math.
2. **T1.4 trade sign autocorrelation = OFI persistence** (ret_autocov [17]). Same phenomenon.
3. **T1.5 tick-rate vol ~ VRR**. Expected rho ~0.7. Not independent.
4. **T2.2 LO arrival/cancel = T1.6 cancellation rate**. Snapshot-inferred, same signal.
5. **T2.6 LOB KE = depth volatility**. Repackaged, killed in R15.
6. **T2.5 spread duration = spread EMA** regime. Already measured.

**Net unique new directions after deduplication: 11** (from 46 proposed).

---

## Part 3: Missing Directions

The surveys are heavily focused on microstructure-derived signals from tick/bidask data. Several important directions are entirely absent.

### Missing Direction 1: Intraday Calendar Effects (Time-of-Day / Day-of-Week)

**Theoretical basis**: TAIFEX has well-known session structure: 8:45 opening auction, 10:00-10:30 lunch lull, 13:30 closing auction, 15:00 after-hours open. Volatility, spread, and mean-reversion rates vary systematically across these periods. R14 already discovered that "opening = momentum, rest = mean-reversion" for CBS. R17 found Gap Fade (C1) with +32 bps at 70.4% WR. Thursday Night Short (C4) showed +467 pts with p=0.003 (tiny N). Day-of-week effects on TMFD6 are completely untested.

**Data requirements**: Existing ClickHouse data with timestamps. Zero new infrastructure.

**Expected horizon**: Session-level (hours). This is a slow-moving gate, not a tick-level signal.

**Why not already covered**: All 7 surveys focus on microstructure (tick/LOB dynamics). Calendar effects are macrostructure. The surveys explicitly state "L1 signals approaching exhaustion" (R20 conclusion) but then propose... more L1 signals. Calendar patterns use a completely different information source (the clock).

**Implementation**: ~30 LOC. `session_phase = f(wall_clock)` with 4-6 discrete phases. CBS parameters (threshold, hold time, stop loss) vary by phase. No model fitting required -- simple conditional analysis on existing backtest results.

**Kill criteria**: If CBS P&L does not differ by > 2x across session phases (p < 0.05), calendar gating adds nothing.

---

### Missing Direction 2: Contract Rollover Dynamics

**Theoretical basis**: TMFD6/TXFD6 are monthly futures contracts. Rollover week (3rd Wednesday expiration) creates predictable patterns: (a) declining open interest in near-month, (b) increasing activity in far-month, (c) basis convergence forcing price toward spot, (d) short-term arbitrageurs rolling positions create transient mispricings. These patterns are well-documented in the futures literature (e.g., Bessembinder et al. 1996) but completely absent from all 7 surveys.

**Data requirements**: ClickHouse historical data across multiple expiration cycles. Need to identify rollover periods (typically 3-5 days before expiration). Our data covers ~3 months = ~3 expiration cycles -- very small sample. But the patterns are structural (not statistical) so even 3 cycles may show clear effects.

**Expected horizon**: Days-level (position over rollover week).

**Why not already covered**: The surveys treat TMFD6 as a generic continuous futures contract. None address the discrete contract lifecycle. Rollover is a purely calendar-driven effect that microstructure models cannot capture.

**Implementation**: ~50 LOC. Track days-to-expiration as a feature. Analyze CBS P&L conditioned on DTE. If rollover week shows systematically different behavior, add DTE-based parameter adjustment.

**Kill criteria**: If CBS P&L in rollover week (DTE < 5) vs. non-rollover week is not statistically different (p < 0.10, two-sample t-test), rollover effects are too weak to exploit.

---

### Missing Direction 3: TXFD6-TMFD6 Spread Dynamics (Intra-Product Basis)

**Theoretical basis**: TXFD6 (large contract, 200 NTD/pt) and TMFD6 (mini, 10 NTD/pt) track the same underlying index but have different participant bases. The TXFD6-TMFD6 basis (price difference normalized by contract multiplier) should be near-zero but can deviate due to differential order flow. If institutional flow hits TXFD6 first (R17 found TMFD6 is MORE liquid, tick ratio 0.65), the basis temporarily deviates, creating a lead-lag signal.

**Data requirements**: We already have TXFD6 in our ClickHouse data (per R17 TSMC lead-lag study). Need concurrent TXFD6 and TMFD6 tick streams.

**Expected horizon**: Seconds to minutes (basis mean-reversion).

**Why not already covered**: R17 tested TSMC stock -> TMFD6 lead-lag (cross-asset) but NOT TXFD6 -> TMFD6 (same underlying, different contract size). This is a fundamentally different and simpler relationship. The basis should be stationary by no-arbitrage.

**Implementation**: ~40 LOC. Compute TXFD6-TMFD6 mid-price ratio. Track deviations from 1.0. Use as CBS entry filter: if basis is stretched (TXFD6 leading), TMFD6 should follow.

**Kill criteria**: If the basis standard deviation is < 0.5 ticks TMFD6, there is no exploitable deviation. Also: if basis half-life < 2 seconds, our 36ms latency cannot capture it.

---

### Missing Direction 4: Options-Derived Volatility Signal (TXO Put-Call Ratio)

**Theoretical basis**: R17 identified TXO options data in ClickHouse (115K ticks, though mostly quotes not trades). The put-call ratio (PCR) is a well-established sentiment indicator. Even from quote data, we can compute the implied PCR from relative bid/ask activity on puts vs. calls. Extreme PCR (> 1.3 or < 0.7) often precedes reversals.

**Data requirements**: TXO quote data in ClickHouse. R17 noted this is available but "99.7% quotes, not trades." Quote-derived PCR is still informative -- market makers set prices that reflect order flow.

**Expected horizon**: 30 min to daily. This is a slow-moving regime indicator.

**Why not already covered**: The surveys focus exclusively on TMFD6/TXFD6 futures tick data. TXO is a different instrument class in the same exchange. R17 explicitly recommended "Fix TXO trade ticks" as R18 prerequisite. R20 concluded "new alpha needs new data sources (trade classification, TXO trades, macro)." The surveys ignored this recommendation entirely.

**Implementation**: ~100 LOC. Compute rolling put OI / call OI from TXO L1 snapshots. Use as CBS daily regime gate. High PCR (fear) + CBS cascade buy signal = stronger contrarian conviction.

**Kill criteria**: If PCR from TXO quotes shows IC < 0.03 for next-day TMFD6 return, the signal is too weak. Also: if TXO quote update frequency is < 1/minute, the data is too sparse.

---

### Missing Direction 5: Global Market Correlation Regime

**Theoretical basis**: TAIEX futures are correlated with global risk sentiment (S&P 500 overnight, VIX, USD/TWD). The overnight session (15:00-5:00 next day) absorbs US market moves. The degree of correlation itself varies over time -- in crisis periods, correlation spikes (contagion). Tracking rolling correlation with ES/SPX overnight futures can identify regime shifts that affect intraday TMFD6 behavior.

**Data requirements**: External data source needed (S&P 500 futures, VIX). Not currently in our pipeline. However, the previous day's close and overnight return are trivially available from public sources and can be ingested as a daily feature.

**Expected horizon**: Daily regime gate.

**Why not already covered**: All surveys are endogenous (using only TAIFEX data). No survey considers exogenous factors. R20 concluded "L1 microstructure EXHAUSTED. New alpha needs new data sources... macro." This is precisely that new data source.

**Implementation**: ~50 LOC for a daily pre-market script that fetches SPX/VIX close, computes overnight gap, and writes a regime flag. CBS uses this as a confidence modifier.

**Kill criteria**: If 30-day rolling correlation between SPX overnight return and TMFD6 opening return is < 0.3, the cross-market signal is too weak. If it is > 0.5 but varies little (std < 0.1), it is a constant offset not a regime indicator.

---

## Part 4: Priority Re-Ranking

All surviving directions ranked by `P(success) * expected_value / implementation_cost`:

| Rank | Direction | P(success) | Expected Value | Cost (LOC) | Score | Rationale |
|------|-----------|------------|----------------|------------|-------|-----------|
| **1** | **Execution Optimizer (T0.2)** | 0.70 | 0.45 pts/trade = 11% RT cost reduction | 150 | **HIGH** | Proven framework (R^2=0.946 on BTC), all data available, independent of alpha quality, reduces cost barrier for ALL future strategies. Not alpha -- direct P&L improvement. |
| **2** | **Trade Classification (T1.1)** | 0.80 | Infrastructure unlock | 100 | **HIGH** | Unlocks 4-5 signal families. Low code cost. Accuracy on TMFD6 is the key uncertainty but downside is bounded (we learn something either way). |
| **3** | **Calendar/Session Effects (NEW)** | 0.60 | CBS Sharpe +20-30% | 30 | **HIGH** | R14 already found opening/rest regime split. Formalizing with systematic test is trivial. Zero model risk. |
| **4** | **VRR (rv_ratio_regime)** | 0.50 | CBS Sharpe +10-15% | 20 | **MEDIUM-HIGH** | R20 prior GO but unexecuted. Orthogonal (rho=0.053). Main risk: RV_5s degeneracy. |
| **5** | **Log-GOFI (T1.7)** | 0.30 | Marginal OFI improvement | 5 | **MEDIUM** | ~5 LOC, zero risk. If it works, free improvement. If not, costs nothing. |
| **6** | **Symmetric Depth Change (from T1.3)** | 0.35 | Net liquidity signal | 30 | **MEDIUM** | The symmetric component is genuinely novel. Antisymmetric component is duplicate. |
| **7** | **Instantaneous Vol (T0.1)** | 0.35 | CBS regime gate | 20 | **MEDIUM** | Formula may degenerate on discrete TMFD6 spread. Partially redundant with VRR. Test after VRR. |
| **8** | **TXFD6-TMFD6 Basis (NEW)** | 0.30 | Intra-product lead-lag | 40 | **MEDIUM** | Simple stationarity argument. Never tested. Quick to validate. |
| **9** | **Contract Rollover (NEW)** | 0.25 | DTE-conditional CBS adjustment | 50 | **MEDIUM-LOW** | Only 3 cycles of data. Pattern may not be detectable with current sample size. |
| **10** | **Hawkes Branching Ratio (T1.2)** | 0.25 | Regime indicator | 50 | **MEDIUM-LOW** | Estimation is noisy on TMFD6 tick rate. Exponential kernel mismatch. May not be independent of VRR. |
| **11** | **Cancellation Rate Asymmetry (T1.6)** | 0.25 | Depth dynamics signal | 40 | **MEDIUM-LOW** | TMFD6 depth too thin for clean rate estimation. May degenerate to binary noise. |
| **12** | **Event-Driven Aggregation (T2.7)** | 0.30 | Better aggregation method | 120 | **LOW** | High implementation cost. Hidden parameter (threshold). Should test AFTER fixed-window aggregation proves useful. |
| **13** | **HAR 3-Window EMA (T0.3)** | 0.20 | Extend tick signals to 30s+ | 100 | **LOW** | 126 features with ~1,540 observations = overfitting guaranteed. Must aggressively prune to < 10 features. The 300s window is likely noise (OFI tau=15s). Defer until we have clear winning features to aggregate. |
| **14** | **Imbalance MR Speed** | 0.20 | Regime gate | 40 | **LOW** | TMFD6 imbalance likely near-binary. OU fit on binary data is degenerate. |
| **15** | **TXO Put-Call Ratio (NEW)** | 0.20 | Daily regime gate | 100 | **LOW** | TXO data is 99.7% quotes. Signal may not exist in quote-only data. Worth investigating but low confidence. |
| **16** | **Global Correlation Regime (NEW)** | 0.20 | Daily macro gate | 50 | **LOW** | Requires external data. Small expected edge. Many confounders. |
| **17** | **Intensity Burst Detection (T2.3)** | 0.20 | Event trigger | 50 | **LOW** | Ambiguous direction (burst = avoid or enter?). Overlaps with VRR. |
| **18** | **Persistent Depth Change (T2.8)** | 0.15 | Filtered signal | 40 | **LOW** | TMFD6 depth changes may ALL be persistent (1-3 lots). No flickering to filter. |

### Directions REJECTED (do not pursue)

| Direction | Reason |
|-----------|--------|
| Trade sign autocorrelation (T1.4) | Requires classification + redundant with OFI persistence |
| Tick-rate vol (T1.5) | Redundant with VRR (rho ~0.7) |
| Metaorder detection (T2.1) | Triple dependency chain, TMFD6 too small for metaorders |
| LO arrival/cancel rate (T2.2) | Duplicate of T1.6 |
| Local Hurst exponent (T2.4) | Requires signed flow, estimation window too long |
| Spread widening duration (T2.5) | TMFD6 spread too discrete, duplicate of spread EMA |
| LOB KE approximation (T2.6) | Killed in R15, repackaged depth volatility |
| Path signatures (Tier 3) | Offline only, not real-time |
| Wavelet decomposition (Tier 3) | High cost, uncertain payoff, HAR is simpler |
| PCA mode decomposition (Tier 3) | Requires L3 data |
| Neural HMM (Tier 3) | Too heavy for tick loop |
| OFI run length (Stage 1) | OFI variant family killed across R11-R19 |

---

## Summary

**From 46 proposed directions + 5 new proposals = 51 total:**
- **18 survive** for investigation (6 APPROVE, 12 CONDITIONAL)
- **33 rejected** (19 redundant/duplicate, 8 data infeasible, 6 already killed in prior rounds)

**Top 3 priorities by expected value:**
1. Execution Optimizer (cost reduction, not alpha)
2. Trade Classification (infrastructure unlock)
3. Calendar/Session Effects (free lunch from R14 finding)

**Structural observation**: The R22 surveys proposed 46 directions to find new *microstructure alpha*. This is the wrong frame. R16-R21 have conclusively demonstrated that L1 microstructure alpha on TMFD6 is exhausted after transaction costs. The highest-value directions are (a) reducing costs (execution optimizer), (b) unlocking new information (trade classification), and (c) exploiting non-microstructure patterns (calendar, rollover, cross-product). More sophisticated LOB features will not overcome the 4-point RT cost barrier.
