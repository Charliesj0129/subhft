# R18 Stage 1 Challenger Review: LOB-Driven Medium-Frequency Survey

**Date**: 2026-03-26
**Reviewer**: Challenger Agent
**Artifact**: `docs/alpha-research/round18_stage1_lob_mf_survey.md`
**Status**: Stage 1 Survey -- 3 candidates (log-GOFI, OFI-OU Regime, ClusterLOB)

---

## VERDICT: CONDITIONAL APPROVE (Candidates A and B only). REJECT Candidate C.

Conditions listed below under each challenge. Candidates A and B may proceed to Stage 2 if conditions are addressed. Candidate C has a fatal data gap that makes it unreplicable on our infrastructure.

---

## Challenge 1: Contemporaneous R-squared is NOT Predictive Alpha (Candidate A -- log-GOFI)

**Claim challenged**: The survey's headline evidence for log-GOFI is "Out-of-sample R-squared for contemporaneous mid-price changes: 83.57% at 30s, 86.01% at 5min" (Su et al. 2021). The survey then states: "R-squared INCREASES with horizon (83.6% to 86.0% from 30s to 5min). The signal gets more explanatory at longer windows -- exactly what we need."

**Objection**: This is a fundamental conflation of **contemporaneous explanation** and **predictive power**. The R-squared measures how much of the price change in window [t, t+T] is explained by log-GOFI computed over the SAME window [t, t+T]. This is NOT a forecast. It says "OFI explains why the price moved" -- which is trivially true since OFI IS the mechanism of price movement (aggressive orders walk the book, creating both the OFI and the price change simultaneously).

The survey itself acknowledges this in Risk #1: "Contemporaneous vs predictive: Paper measures R-squared in same window, not t to t+1 prediction." Yet despite this acknowledgment, the survey uses the 83-86% R-squared as the primary evidence for approval. **This is circular**: the survey identifies the exact weakness, then proceeds to approve based on the very metric it just called into question.

**Prior round evidence that this fails**: R9-R11 and R16 all found that OFI has high contemporaneous correlation with returns but **weak predictive power at horizons > 15s**. Memory states: "Signal-horizon mismatch: signals work 5-15s, costs need 60s+ where signals are dead." The survey's own exclusion list acknowledges "L1 OFI / depth imbalance: IC decays <15s, signal-horizon mismatch." log-GOFI is still fundamentally OFI -- aggregated across more levels and log-transformed, but still measuring the same flow-price mechanism. The stationarization may help, but the survey provides zero evidence that it creates predictive persistence.

Takahashi (2025), which the survey itself cites in the exclusion list, explicitly found "Shocks dissipate within a second" on E-mini. The claim that multi-level aggregation plus log transform fundamentally changes the decay profile is unsubstantiated.

**Data required to resolve**:
1. Compute log-GOFI on TXFD6/TMFD6 historical data. Measure **predictive** IC: correlation between log-GOFI(t) and returns(t+T) for T = 1min, 5min, 15min, 30min. NOT contemporaneous.
2. If predictive IC at 5min < 0.043 (30-min breakeven from R17 cost model), kill immediately.
3. Report autocorrelation of log-GOFI itself at lag 1min, 5min, 30min. If autocorrelation < 0.3 at 5min, the signal has no persistence and cannot be used as a medium-frequency predictor.

**Severity**: CRITICAL. The entire approval premise rests on a metric that does not measure what we need. Without predictive IC evidence on our instruments, this is an OFI variant that will repeat R9-R16 failures.

---

## Challenge 2: OFI-OU Regime (Candidate B) -- CSI 300 Correlation Persistence Does Not Transfer to TMFD6

**Claim challenged**: The survey cites OFI correlation > 0.50 stable from 5s to 60min on CSI 300 Index Futures (Hu & Zhang 2025), calling this "extraordinary persistence if it transfers."

**Objection 1 -- Instrument mismatch**: CSI 300 Index Futures are among the most liquid futures contracts in the world (daily volume ~400K contracts, ~$50B notional). TMFD6 Mini-TAIEX is a micro contract with 1.8 ticks/sec. The liquidity differential is 100-1000x. OFI persistence is a function of market depth, participant diversity, and metaorder splitting. On deep, diverse markets, large institutional flows create persistent OFI because orders are split over time. On thin retail-dominated markets like TMFD6, individual 1-lot orders create impulse OFI that dissipates immediately.

**Our own data directly contradicts the persistence claim**: R16 found "OFI signals work 5-15s but die at 60s+ on TMFD6." The survey acknowledges this in Risk #2 but does not explain why the OU regime framework would overcome this instrument-specific finding. The OU model's "regime-dependent memory" is calibrated on CSI 300 where memory EXISTS. If TMFD6 has no OFI memory beyond 15s, the OU model will correctly estimate kappa as very large (fast mean-reversion), and the optimal holding period will collapse to seconds -- exactly where we already know signals die before costs.

**Objection 2 -- Contemporaneous correlation again**: The survey presents the OFI correlation table (0.20 at 0.5s to 0.54 at 1h). The paper (Hu & Zhang 2025) computes these as **within-window** correlations (OFI over [t, t+T] vs return over [t, t+T]), not predictive correlations. This is the same contemporaneous-vs-predictive conflation as Challenge 1. The survey's key gate "OFI corr > 0.10 at 5-minute horizon" needs clarification: is this contemporaneous (trivially satisfied) or predictive (the real test)?

**Data required to resolve**:
1. Measure OFI autocorrelation on TXFD6 and TMFD6 at lags 30s, 1min, 5min, 15min, 30min. If autocorrelation < 0.1 at 5min, there is no "memory" to exploit and the OU framework adds nothing over existing OFI features.
2. Measure **predictive** OFI correlation: correlation of OFI(t-T, t) with return(t, t+T). Not within-window. Must use strictly non-overlapping intervals.
3. If predictive correlation at 5min < 0.10 on TXFD6/TMFD6, kill immediately. Do not proceed to OU fitting or regime detection.
4. Report the R16 OFI decay curve (IC vs horizon) as a direct comparator. The Researcher must explain what mechanism in log-GOFI or OU-regime overcomes the known 15s decay.

**Severity**: HIGH. The 0.50+ correlation is almost certainly contemporaneous, which is uninformative for a trading strategy. Without predictive evidence on our instruments, this repeats R16.

---

## Challenge 3: Candidate C (ClusterLOB) Has a Fatal Data Gap -- REJECT

**Claim challenged**: The survey gives Candidate C a "CONDITIONAL APPROVE (secondary priority)" despite identifying a "CRITICAL GAP: Cannot directly replicate clustering."

**Objection**: The ClusterLOB paper (Zhang et al. 2025) requires Market-By-Order (MBO) data with individual order IDs, types, and lifetimes. We have LOB snapshots (aggregate state). The survey proposes a "workaround: classify LOB state changes as aggressive vs passive." This is not a workaround -- it is a fundamentally different signal. The paper's entire contribution is that **individual order classification** into directional/opportunistic/MM clusters reveals hidden structure that aggregate OFI misses. If you classify at the aggregate level, you are not implementing ClusterLOB -- you are implementing yet another OFI variant, which we have tested exhaustively in R9-R16.

Furthermore:
1. **K-means on aggregate LOB changes** is not methodologically sound. The paper clusters on 6 per-order features (order size, aggressiveness, cancellation rate, lifetime, etc.). Aggregate LOB changes conflate all participants. An "aggressive-looking" state change could be 10 small retail orders, not 1 institutional sweep.
2. **NASDAQ equities with MBO data vs Taiwan futures with LOB snapshots**: Even if we could approximate the clustering, the paper was validated on NASDAQ MBO data where cluster separation is driven by HFT/institutional participant diversity. TMFD6 is retail-dominated with no MBO data -- there may not be meaningful clusters to find.
3. **The paper does not report absolute PnL after costs** (survey's own Risk #3). Without absolute PnL, we cannot assess whether the Sharpe improvement justifies the implementation cost, especially with our 4-pt RT constraint.

**Verdict**: REJECT. Do not proceed to Stage 2. The data gap is not bridgeable, the proposed approximation is a different signal, and the original paper lacks cost-adjusted PnL evidence. If MBO data becomes available in the future, this can be revisited.

**Severity**: FATAL for Candidate C.

---

## Challenge 4: All Three Candidates Are OFI Variants -- Where Is the Differentiation?

**Claim challenged**: The survey presents three candidates as distinct directions: "Multi-level aggregated OFI + log stationarization" (A), "OU shock model + regime switch" (B), "Participant-decomposed OFI" (C).

**Objection**: Strip away the mathematical dressing and all three are:
- **A**: A fancier way to compute OFI (across multiple levels, log-transformed)
- **B**: A fancier way to normalize OFI (regime-adaptive z-score based on OU parameters)
- **C**: A fancier way to decompose OFI (by participant type)

The survey's "Prior Round Exclusion List" states "L1 OFI / depth imbalance: IC decays <15s, signal-horizon mismatch." The implicit claim is that these OFI variants are fundamentally different from "L1 OFI." But:

- **MLOFI gradient** (multi-level OFI) was already tested in R11: "IC=-0.105 significant, Gate C FAIL (fees > returns)." log-GOFI is a log-transformed generalization of the same concept. The survey does not explain why log transformation overcomes the cost barrier that killed MLOFI.
- **OFI regime switching** was conceptually tested in R12 (VPIN regime overlay, -30.6% DD) and the older paper intake (Candidate 3: `ofi_regime_memory`). Both failed. The survey does not differentiate from these prior attempts.
- **ofi_depth_norm_ppm** is already feature [16] in FE v2 and was "PASS as feature, FAIL as standalone" (memory). Adding more OFI variants to the feature engine is fine, but presenting them as standalone alpha candidates when prior OFI variants consistently fail on cost is misleading.

**Data required to resolve**:
1. For Candidate A: report correlation between log-GOFI and existing `ofi_l1_cum` [12] and `ofi_depth_norm_ppm` [16]. If correlation > 0.7, this is not a new signal -- it is a redundant OFI variant.
2. For Candidate B: report correlation between OU-regime z-score and existing `ofi_l1_ema8` [13]. The EMA is already an adaptive smoothing. Explain what the OU framework adds beyond a different smoothing kernel.
3. Explicitly acknowledge: the R18 LOB-MF survey is testing "improved OFI construction methods," not fundamentally new signal sources. Frame expectations accordingly.

**Severity**: MEDIUM-HIGH. This is about honest framing. If these are OFI improvements, the hurdle is: does the improvement overcome the 15s decay barrier that killed standard OFI? If they cannot demonstrate predictive power at 5+ minutes where standard OFI is dead, the construction method is irrelevant.

---

## Challenge 5: Missing Cost Model for Medium-Frequency Trading

**Claim challenged**: The survey states holding periods of 5-15 min (A) and 5-30 min (B), with kill gate "predictive IC > 0.043 at 30-minute horizon." No cost model for the taker entry/exit is presented.

**Objection**: The survey does not specify whether these strategies use market orders (taker) or limit orders (maker) for entry and exit. For medium-frequency directional signals:

1. **If taker**: RT cost = 3.92 pts on TMFD6 (1.19 bps) or 39.2 NTD on TXFD6 (2.0 bps). The R17 cost model established IC breakeven of 0.043 at 30min and 0.030 at 60min. The survey uses the 30min figure but does not specify the instrument. On TXFD6 (higher cost), the breakeven IC is higher.

2. **If maker**: Introduces fill uncertainty, queue position risk, and the entire adverse selection problem that the parallel TMFD6 MM survey (R18 stage1_survey.md) is investigating. The LOB-MF survey does not discuss this at all.

3. **Turnover**: A 5-15 minute holding signal generates ~20-50 roundtrips per session. At 3.92 pts RT cost on TMFD6, that is 78-196 pts daily cost. The signal must generate > 200 pts/day in gross alpha to be net positive. This translates to ~10 pts average profit per trade -- a very high bar given that TMFD6's daily range is often < 100 pts.

4. **Slippage**: At 1.8 ticks/sec on TMFD6, a market order placed at decision time fills 36ms later. At 1-2 ticks during that window, slippage = 1-2 pts per side, adding 2-4 pts to effective RT cost (total 6-8 pts). This is not modeled.

**Data required to resolve**:
1. Specify execution method: taker or maker for each candidate.
2. If taker: compute net IC after slippage on TMFD6 (add 2 pts per side to cost model).
3. If maker: defer to TMFD6 MM survey findings (adverse selection, fill rate).
4. Report expected trade frequency per session. If > 20 RT/day, the cumulative cost is substantial and must be modeled.
5. Specify target instrument: TXFD6 (higher cost, higher liquidity) or TMFD6 (lower cost, lower liquidity). The survey mentions both but does not commit.

**Severity**: HIGH. A directional signal without a cost model is half a strategy. The omission is especially concerning given that R12-R17 all failed on cost structure.

---

## Challenge 6: "Reviewed ~120 results, downloaded and read 3 full papers" -- Insufficient Depth

**Claim challenged**: The search methodology states "Queried arXiv with 10+ variations. Reviewed ~120 results, downloaded and read 3 full papers."

**Objection**: For a survey covering medium-frequency LOB-driven strategies -- a well-studied area with hundreds of papers -- reading only 3 full papers is insufficient for confidence. Key gaps:

1. **No Cartea, Jaimungal, or Lehalle papers** in this survey (they appear in the parallel MM survey but not here). These authors are the leading researchers on LOB-driven signals and optimal execution at medium frequencies.
2. **No Bouchaud, Bonart, or Mastromatteo** (price impact propagator literature). The propagator model directly addresses OFI predictive decay -- exactly the mechanism that matters for medium-frequency OFI signals.
3. **No Toth et al. (2011)** "Anomalous price impact and the critical nature of liquidity" -- foundational paper on why OFI impact is concave and transient, directly relevant to whether any OFI signal can persist to 5+ minutes.

The 3 papers read (Su 2021, Hu 2025, Zhang 2025) are all relatively recent and from Chinese/Oxford groups. This creates a narrow perspective. The foundational literature on OFI decay and price impact transience is not represented.

**Resolution required**: The Researcher should at minimum address: does the Bouchaud propagator model predict that any OFI construction can have persistent predictive power at 5+ minutes? If the propagator decays to zero by 60s (as empirically observed on most liquid futures), then no OFI variant -- however cleverly constructed -- will have predictive power at the target horizon.

**Severity**: MEDIUM. The survey may have implicitly filtered these out, but the absence of foundational impact-decay literature weakens the theoretical case.

---

## Summary Table

| # | Challenge | Severity | Target | Resolution |
|---|-----------|----------|--------|------------|
| 1 | Contemporaneous R-sq != predictive alpha | CRITICAL | A (log-GOFI) | Measure predictive IC at 5/15/30 min on TXFD6/TMFD6 |
| 2 | CSI 300 persistence does not transfer to TMFD6 | HIGH | B (OFI-OU) | Measure OFI autocorrelation and predictive corr on our instruments |
| 3 | Fatal MBO data gap | FATAL | C (ClusterLOB) | REJECT -- data not available, approximation is different signal |
| 4 | All candidates are OFI variants | MEDIUM-HIGH | All | Report correlation with existing OFI features; honest framing |
| 5 | Missing cost model | HIGH | A, B | Specify execution method, instrument, slippage model |
| 6 | Insufficient literature depth | MEDIUM | All | Address propagator/impact-decay literature |

---

## Conditions for Approval (Candidates A and B only)

1. **MANDATORY**: Stage 2 must lead with **predictive IC measurement** on TXFD6/TMFD6 historical data. NOT contemporaneous R-squared. Kill gate: predictive IC < 0.03 at 5-minute horizon.

2. **MANDATORY**: Measure OFI autocorrelation on our instruments at 1min, 5min, 30min lags. If autocorrelation < 0.1 at 5min, neither candidate has the persistence needed for medium-frequency trading. Kill immediately.

3. **MANDATORY**: Report correlation of log-GOFI and OU-z-score with existing FE v2 OFI features (`ofi_l1_cum`, `ofi_depth_norm_ppm`, `ofi_l1_ema8`). If r > 0.7 with any existing feature, the candidate adds insufficient new information.

4. **MANDATORY**: Specify cost model -- taker or maker, instrument (TXFD6 or TMFD6), slippage estimate, expected trade frequency.

5. **RECOMMENDED**: Candidate C (ClusterLOB) should be dropped. If the Researcher wants to pursue participant decomposition, it should be scoped as a separate infrastructure project (MBO data pipeline) rather than an R18 alpha candidate.

6. **RECOMMENDED**: Address the propagator/impact-decay literature. Explain the theoretical mechanism by which log-GOFI or OU-regime achieves predictive persistence beyond the ~15s horizon where standard OFI decays.

---

*Challenger Agent -- R18 Stage 1 LOB-MF Review*
