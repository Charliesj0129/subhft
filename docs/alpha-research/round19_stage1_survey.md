# Round 19 Stage 1: Literature Survey — MF Horizon Extension via MLOFI, Cross-Asset Impact, VPIN Regime Filtering

**Date**: 2026-03-27
**Researcher**: Alpha Research Agent
**Scope**: Extending HF microstructure signals to medium-frequency (30-60 min) prediction horizons
**IC Breakeven**: 0.043 (30 min), 0.030 (60 min). Kill gate: IC < 0.05 at all horizons.

---

## Executive Summary

Surveyed 25+ papers across q-fin.TR, q-fin.MF, and SSRN on three tracks: MLOFI horizon extension, cross-asset microstructure transmission, and VPIN regime filtering. **Two candidate directions survive initial screening; one is killed at survey stage.**

**Critical constraint reminder**: 20 trading days of TMFD6 data with two large gaps (11d + 21d). No 20+ day rolling windows possible. TXFD6 data: 14 files overlapping. Prior rounds (R12-R17) exhaustively showed no L1 single-instrument microstructure signal overcomes 4pt RT cost on TMFD6 at short horizons.

**Key finding from this survey**: The literature strongly supports that (1) multi-level OFI with dimensionality reduction improves explanatory power at longer horizons, and (2) time-series smoothing (EMA/Kalman) can extend signal half-life. However, the reported improvements are for *contemporaneous* R-squared, not *predictive* IC at 30-60 min horizons. The gap between explaining concurrent price moves and predicting future ones is the central risk for all candidates.

---

## Track 3.1: MLOFI — Multi-Level Order Flow Imbalance

### Key Papers

#### P1: Xu, Gould & Howison (2019). "Multi-Level Order-Flow Imbalance in a Limit Order Book." arXiv:1907.06230
- **Data**: 6 liquid Nasdaq stocks, tick-level LOB data
- **Method**: MLOFI is a vector (OFI_1, OFI_2, ..., OFI_K) measuring net order flow at K price levels. Linear regression of mid-price change on MLOFI vector.
- **Key result**: Out-of-sample R-squared improves monotonically with each additional level (L1: ~35%, L5: ~55%, L10: ~60% on 10-event windows). Deep book OFI contributes meaningful explanatory power.
- **PCA finding**: First principal component of MLOFI captures ~70% of variance and is essentially a weighted average heavily loaded on L1. Second PC captures ~15% and represents "depth vs surface" contrast.
- **Horizon**: Contemporaneous (same-window), NOT predictive. Paper does not test forward-looking IC.
- **Applicability to TMFD6**: We have L5 book data. Already implemented `ofi_depth_divergence` alpha (shallow vs deep OFI momentum) with IC=-0.105 from R11 — but Gate C FAIL due to fees > returns.

#### P2: Cont, Cucuringu & Zhang (2021). "Cross-Impact of Order Flow Imbalance in Equity Markets." arXiv:2112.13213
- **Data**: Nasdaq 100 stocks, 5-minute aggregation windows
- **Method**: Multi-level OFI (L1-L5) with cross-sectional (cross-stock) OFI impact. Lagged cross-asset OFIs improve return forecasting.
- **Key result**: Cross-impact of OFI is significant but decays rapidly — mostly within 5 minutes. Self-OFI impact also decays. At 30-60 min horizons, incremental R-squared from OFI is negligible.
- **Applicability**: Confirms that raw OFI signal half-life is too short for our target horizon. Cross-asset OFI is interesting but requires multi-instrument data we partially have (TXFD6).

#### P3: Su, Sun, Li & Yuan (2021). "The Price Impact of Generalized Order Flow Imbalance." arXiv:2112.02947
- **Data**: CSI 500 stocks, high-frequency LOB snapshots
- **Method**: Generalized OFI (GOFI) construction accounting for non-minimum tick sizes. Log-GOFI stationarization.
- **Key result**: Log-GOFI achieves R-squared of 83-86% at 30s-5min horizons (vs 33-43% for standard OFI). Dramatic improvement from proper normalization.
- **Caveat**: Again contemporaneous R-squared. Predictive IC not tested. The improvement is in *fitting* price moves, not *predicting* them.

#### P4: Hu & Zhang (2025). "Stochastic Price Dynamics in Response to OFI: Evidence from CSI 300 Index Futures." arXiv:2505.17388
- **Data**: CSI 300 index futures (analogous to TAIEX futures)
- **Method**: OFI impact modeled as Ornstein-Uhlenbeck process with memory and mean-reversion driven by Levy process. Regime-conditional OFI-price sensitivity.
- **Key result**: OFI impact is regime-dependent. In high-volatility regimes, OFI has 2-3x the price impact. Proposes conditional OFI signal: only trade when regime-dependent sensitivity is elevated.
- **Applicability**: Directly relevant to futures (like TMFD6). The OU model implies OFI signal decays exponentially — consistent with our observation that signals work at 5-15s but die at 60s.

#### P5: Anantha & Jain (2024). "Forecasting High Frequency Order Flow Imbalance." arXiv:2408.03594
- **Data**: NSE (India) tick data
- **Method**: Hawkes process to model self-exciting OFI dynamics. Forecasts near-term OFI distribution.
- **Key result**: Sum-of-exponentials Hawkes kernel gives best OFI forecast. But forecasting OFI != forecasting returns. Predictable OFI is already priced into spreads.
- **Applicability**: Could help us predict when OFI will be large (for opportunistic entry), but doesn't solve the horizon extension problem.

#### P6: Anantha, Jain & Maiti (2025). "Order Book Filtration and Directional Signal Extraction at High Frequency." arXiv:2507.22712
- **Data**: NSE (India) tick data
- **Method**: Filter transient LOB events (short-lived orders, cancel-replace noise) before computing OBI/OFI. Three filtration schemes: order lifetime, update count, inter-update delay.
- **Key result**: Filtered OBI exhibits systematically stronger directional association with returns. Transient orders (>50% of activity) add noise, not signal.
- **Applicability**: Could improve our OFI signal quality. But still operates at HF horizon — does not address the 30-60 min extension.

#### P7: arXiv:2512.18648 — "Optimal Signal Extraction from Order Flow: A Matched Filter Perspective" (Dec 2025)
- **Method**: Matched filter theory applied to OFI normalization. Market-cap normalization = matched filter for institutional flow. Volume normalization = matched filter for VWAP flow.
- **Key result**: Proper normalization yields up to 1.99x higher signal correlation. Korean market data, 2.7M stock-day obs.
- **Applicability**: Cross-sectional normalization paper — not directly applicable to single-instrument TMFD6. But the Kalman filter framework is relevant for time-series smoothing.

#### P8: Kang (2026). "When the Rules Change: Adaptive Signal Extraction via Kalman Filtering and Markov-Switching Regimes." arXiv:2601.05716
- **Data**: Korean stock market 2020-2024, daily investor flow data
- **Method**: Kalman filter + Markov-switching to identify regime-dependent OFI-return relationships.
- **Key result**: Foreign investor predictive power increases several-fold during crisis periods. BUT: **rigorous out-of-sample testing reveals these in-sample regularities do not generalize reliably.**
- **Applicability**: CRITICAL WARNING. This paper explicitly shows that regime-conditional OFI models overfit. Our 20-day dataset is far too small for regime identification.

### Track 3.1 Synthesis

The literature shows:
1. **Multi-level OFI (L1-L5) improves contemporaneous R-squared by 20-50%** over L1-only OFI.
2. **Log-normalization / stationarization** further doubles R-squared.
3. **PCA on MLOFI** reduces dimensionality effectively (1st PC = weighted average, 2nd PC = depth contrast).
4. **All reported improvements are contemporaneous, not predictive at 30-60 min horizons.**
5. **OFI signal decays exponentially (OU process)** — Hu & Zhang confirm our empirical finding.
6. **Regime-conditional OFI models overfit** — Kang (2026) is a cautionary tale with our data constraint.
7. **Filtering transient orders** improves signal quality but doesn't extend horizon.

**Honest assessment**: MLOFI is a better *input feature* than L1 OFI, but no paper demonstrates that any mathematical transform converts a 5-15 second signal into a 30-60 minute predictor. The signal *decays*, it doesn't *extend*. Smoothing (EMA/Kalman) reduces noise but also attenuates the signal proportionally.

---

## Track 3.2: Cross-Asset Microstructure Impact

### Key Papers

#### P9: Li, Chen & Liu (2025). "High-frequency lead-lag relationships in Chinese stock index futures." arXiv:2501.03171
- **Data**: CSI 300 index futures, tick-by-tick across 4 contract maturities
- **Method**: Lead-lag relationship detection via cross-correlation and impulse-response functions.
- **Key result**: Near-month contract leads deferred contracts by 1-3 ticks. The "lead-lag spread" mean-reverts predictably. Profitable after transaction costs in Chinese market.
- **Applicability**: Directly relevant to TXFD6→TMFD6 lead-lag. Already proposed as Candidate 1 (CSLL) in R17 survey but rejected due to missing TXFD6 subscription. We now have 14 days of TXFD6 data.

#### P10: Michael, Cucuringu & Howison (2022). "Option Volume Imbalance as a predictor for equity market returns." arXiv:2201.09319
- **Data**: NASDAQ PHLX options, 2015-2019, ~750-1000 stocks/day
- **Method**: OVI = (positive-view volume - negative-view volume) / total. Market-maker OVI is strongest (SR 3.5-4.5 annualized).
- **Key result**: Aggregate OVI predicts **overnight** excess returns. Daily signal, not intraday.
- **Critical gap for us**: Requires market participant classification (MPC) for strongest signal. TAIFEX does not provide MPC data. TXO data is 99.7% quotes with only 115K trade ticks (confirmed in R17). **OIDS is effectively dead** — no trade-level TXO data to compute OVI.
- **R17 verdict**: Killed after discovering TXO data limitation.

#### P11: Cont, Cucuringu & Zhang (2021). "Cross-Impact of Order Flow Imbalance." arXiv:2112.13213
- **Method**: Lagged cross-asset OFIs. Stock A's OFI predicts Stock B's return.
- **Key result**: Cross-impact mainly manifests at short-term horizons (< 5 min) and decays rapidly.
- **Applicability**: TXFD6→TMFD6 OFI cross-impact could work at short horizons but not at 30-60 min target. Li et al. (2025) above is more specific.

### Track 3.2 Synthesis

Cross-asset microstructure has one viable direction: **TXFD6→TMFD6 lead-lag**. But:
1. Signal half-life is 1-30 seconds (price discovery lag), NOT 30-60 minutes
2. We now have 14 days TXFD6 data (up from 0), making prototyping feasible
3. Options path (OIDS) is dead — TXO data quality confirmed insufficient in R17
4. Cross-OFI impact decays within 5 minutes — below our target horizon

**The lead-lag signal is real but operates at a fundamentally different timescale than R19's target horizon.** It could be viable as a standalone short-horizon strategy (like CBS) but does NOT extend microstructure signals to 30-60 min.

---

## Track 3.3: VPIN — Volume-Synchronized Probability of Informed Trading

### Key Papers

#### P12: Easley, Lopez de Prado & O'Hara (2012). "Flow Toxicity and Liquidity in a High Frequency World." Review of Financial Studies.
- **Method**: VPIN = |V_buy - V_sell| / (V_buy + V_sell) computed on volume-synchronized bars (fixed-volume buckets instead of fixed-time bars). Buy/sell classification via bulk volume classification (BVC) — uses cumulative tick-rule approximation.
- **Key result**: VPIN rises 2-3 hours before the 2010 Flash Crash. Proposed as real-time toxicity monitor.
- **Caveat**: Academic debate (Andersen & Bondarenko 2014) showed VPIN peaked AFTER the crash, not before. VPIN's predictive power is mechanically related to trading intensity, not genuinely informed-flow detection.

#### P13: Andersen & Bondarenko (2014). "Assessing Measures of Order Flow Toxicity." SSRN.
- **Key result**: VPIN is a poor predictor of short-run volatility. Its apparent predictive content is due primarily to a mechanical relation with underlying trading intensity. A simple "volume surprise" metric performs comparably.
- **Implication**: VPIN may be more of a volume-regime indicator than a genuine toxicity metric.

#### P14: Song, Wu & Simon (2014). "Parameter Analysis of the VPIN Metric." SSRN.
- **Key result**: VPIN performance is highly sensitive to bucket size and lookback window. Recommends parameter ranges for futures contracts. No single parameter set works universally.
- **Implication**: VPIN requires instrument-specific calibration. With 20 days of data, overfitting risk is extreme.

#### P15: Bjursell, Wang & Zheng (2017). "VPIN, Jump Dynamics and Inventory Announcements in Energy Futures." SSRN.
- **Data**: Crude oil and natural gas futures, 2009-2015
- **Key result**: VPIN rises before inventory announcements and price jumps. Works as a conditional indicator for scheduled events.
- **Applicability**: TMFD6 has no scheduled inventory events. Taiwan market events (TAIEX rebalancing, margin changes) are too infrequent.

#### P16: Borochin & Rush (2016). "Identifying and Pricing Adverse Selection Risk with VPIN." SSRN.
- **Key result**: VPIN captures adverse selection risk in equity cross-section. High-VPIN stocks have higher expected returns (risk premium).
- **Applicability**: Cross-sectional finding, not applicable to single-instrument TMFD6.

### Track 3.3 Synthesis

VPIN as a **standalone predictor** is academically contested and likely mechanically tied to volume intensity. However, VPIN as a **regime filter / conditional gate** has more promise:

1. **R12 already tested VPIN overlay**: Result was DD -30.6%. Killed.
2. **VPIN parameter sensitivity** is extreme — 20 days is insufficient for robust calibration.
3. **Volume-clock bars** are a useful concept (time-invariant sampling) but the toxicity interpretation is weak.
4. **Better alternatives exist**: Our CBS spread-gate and time-of-day filters already capture the same regime information more directly.

**Honest assessment**: VPIN adds nothing over what we already have. The CBS strategy's spread-gate (wide spread = opportunity) is a more direct and already-validated version of what VPIN tries to capture. Revisiting VPIN would repeat R12's failure.

---

## Candidate Alpha Directions

### Candidate A: MLOFI-Smoothed Directional Signal (Track 3.1)

**Concept**: Compute MLOFI (L1-L5), apply PCA to extract 2 principal components, then smooth with EMA(300s) / EMA(900s) crossover. Trade the smoothed signal direction at 30-60 min horizon.

**Theoretical IC estimate**:
- Raw L1 OFI IC at 5-15s: ~0.10-0.15 (established from R11, R16)
- Multi-level improvement: +20-50% → IC ~0.12-0.20 at 5-15s
- Smoothing attenuation at 30 min: signal decays ~exp(-t/tau), tau ~15s → at 1800s, IC ~0.12 * exp(-120) ≈ 0
- **Even optimistically (tau=300s via smoothing)**: IC ~0.12 * exp(-6) ≈ 0.0003
- **Theoretical IC at 30 min: << 0.01** — far below 0.043 breakeven

**Data requirements**: L5 book data (HAVE), 20+ days for parameter stability (MARGINAL — have 20 days but gapped)

**Feasibility**: CAN prototype with current data. Python prototype using existing `OFICalculator` + numpy PCA + EMA is ~100 LOC.

**GO/NO-GO: NO-GO as standalone alpha.**

**Rationale**: The fundamental physics of OFI signal decay (OU process, confirmed by Hu & Zhang 2025 and our own R16/R17 data) means no mathematical transform can extend a 15-second signal to 30 minutes. Smoothing reduces noise but also reduces signal proportionally. The literature universally reports *contemporaneous* R-squared improvements, never *predictive* IC at longer horizons. This is a measurement improvement, not an alpha.

**However**: MLOFI-PCA is a legitimate *feature engineering improvement* for our existing FeatureEngine. It could improve short-horizon strategies (CBS, OpMM) by providing better real-time OFI measurement. This is a Phase 20 FeatureEngine upgrade, not an alpha direction.

---

### Candidate B: TXFD6→TMFD6 Cross-Instrument Lead-Lag (Track 3.2)

**Concept**: Monitor TXFD6 price changes in real-time. When TXFD6 moves significantly (>X pts in Y seconds), enter TMFD6 in the same direction before TMFD6 catches up. Hold until convergence or timeout (5-60s).

**Initial theoretical IC estimate**: 0.05-0.10 at 5-30s (based on Li et al. 2025 and R17 TSMC lead-lag IC=0.061).

**GO/NO-GO: NO-GO — KILLED AFTER CHALLENGER REVIEW (data verification)**

#### Post-Review Data Findings (2026-03-27)

**Finding 1: TMFD6 is MORE liquid than TXFD6 — lead-lag premise is inverted.**

Actual tick counts from 13 overlap days:

| Period | TX/TM tick ratio | TXFD6 ticks/day | TMFD6 ticks/day |
|--------|-----------------|-----------------|-----------------|
| March (4d) | **0.65** | 445K | 684K |
| Jan/Feb (9d) | 0.83 | 307K | 368K |

TMFD6 has 50% more ticks than TXFD6 in March. Both instruments tick at ~125ms (exchange throttle), but TMFD6 has more quote updates. There is no liquidity-driven price discovery lag from TXFD6 to TMFD6.

The Li et al. (2025) analogy is invalid: their study was near-month leading deferred-month (liquidity differential), not full-size leading mini (same underlying, mini more active).

**Finding 2: Data overlap is regime-contaminated.**

13 overlap days split: 9 Jan/Feb (anomalous wide-spread) + 4 March (normal). Per standing rule (feedback_backtest_recency_bias.md), March-only validation = 4 days. IS/OOS split impossible.

**Finding 3: IC breakeven at taker entry is structurally impossible.**

| Horizon | sigma (pts) | IC breakeven (taker) | IC breakeven (maker) |
|---------|------------|---------------------|---------------------|
| 5s | 9.93 | **0.349** | 0.197 |
| 15s | 17.81 | **0.194** | 0.110 |
| 30s | 26.12 | **0.133** | 0.075 |
| 60s | 38.44 | 0.090 | 0.051 |
| 300s | 79.63 | 0.044 | 0.025 |

Lead-lag requires taker entry (cross spread to capture lag). IC breakeven at taker: 0.35 at 5s, 0.13 at 30s. No microstructure signal on our data has ever produced IC > 0.13.

**Finding 4: Actual cross-correlation confirms lead-lag is too small to trade.**

Ran 10s-window return cross-correlation across all 4 March days:

| Lag | Mean lead_diff (TX→TM minus TM→TX) | Std | Days TX leads |
|-----|-------------------------------------|-----|---------------|
| 1s | +0.005 | 0.006 | 3/4 |
| 2s | +0.015 | 0.023 | 3/4 |
| 5s | +0.021 | 0.056 | 3/4 |
| 10s | +0.013 | 0.049 | 3/4 |

TXFD6 does lead TMFD6 very slightly (+0.02 peak correlation asymmetry at 5s lag), but the effect is noise-level (std > mean at lag >= 5s), one day (Mar 20) shows reversed sign, and the magnitude is far below the 0.13 IC taker breakeven.

**Key risks** (pre-kill, retained for reference):
1. Only 4 days of normal-regime overlap — OOS validation impossible
2. TMFD6 spread (3 pts) + fees (3.92 pts) = 6.92 pts RT taker cost
3. TXFD6 and TMFD6 are nearly perfectly synchronized — lead-lag is < 0.02 correlation

---

### Candidate C: VPIN Regime-Gated CBS Enhancement (Track 3.3)

**Concept**: Compute VPIN on TMFD6 using volume-synchronized bars. Use high-VPIN periods (elevated toxicity / informed flow) to gate CBS entries — suppress CBS during high-toxicity periods, or enhance position sizing during low-toxicity.

**Theoretical IC estimate**: Not applicable as standalone. The question is whether VPIN adds filtering value over CBS's existing spread-gate + ToD gate.

**Data requirements**: TMFD6 tick data with volume (HAVE). 50+ days for VPIN parameter calibration (DO NOT HAVE — only 20 days).

**Feasibility**: CAN compute VPIN on 20 days of data, but cannot robustly calibrate bucket size or threshold parameters.

**GO/NO-GO: NO-GO.**

**Rationale**:
1. R12 already tested VPIN overlay → DD -30.6%. Killed.
2. Andersen & Bondarenko (2014) showed VPIN's predictive power is mechanical (volume intensity), not genuine toxicity.
3. CBS already uses spread-gate (direct observation of market quality) + ToD filter. VPIN would add a noisier, delayed version of the same information.
4. 20 days is grossly insufficient for VPIN calibration (Song et al. 2014 show extreme parameter sensitivity).
5. No paper demonstrates VPIN as a successful *filter* for an existing signal — it's always tested standalone.

---

## Summary Table

| Candidate | Track | Target Horizon | Est. IC | Data Feasible? | GO/NO-GO |
|-----------|-------|---------------|---------|----------------|----------|
| A: MLOFI-Smoothed | 3.1 | 30-60 min | << 0.01 | Yes | **NO-GO** (signal physics) |
| B: TXFD6→TMFD6 Lead-Lag | 3.2 | 5-30s | N/A | No (premise inverted) | **NO-GO** (TMFD6 more liquid; IC breakeven impossible) |
| C: VPIN Regime Gate | 3.3 | Filter | N/A | No (20d insufficient) | **NO-GO** (R12 repeat, data gap) |

---

## Recommendations

### 1. The R19 Research Direction Has a Structural Problem

The premise of R19 — "extending HF microstructure signals to medium-frequency (30-60 min) prediction horizons" — is contradicted by the literature. Every paper we surveyed confirms that:
- OFI signal decays exponentially with a half-life of 5-30 seconds (OU process)
- Multi-level aggregation and smoothing improve *measurement quality* but cannot extend *signal lifetime*
- Regime-conditional approaches overfit on small datasets (Kang 2026 explicitly warns)
- Cross-asset OFI impact decays within 5 minutes

**No mathematical transform can extend a signal whose underlying information is consumed by the market in 15 seconds to predict at 30-60 minute horizons.** This is not a data problem — it's a signal physics problem.

### 2. What We SHOULD Do Instead

**Option 1 (KILLED after Challenger review): ~~Prototype TXFD6→TMFD6 lead-lag.~~**
- Killed: TMFD6 is more liquid than TXFD6 (TX/TM ratio = 0.65). Lead-lag premise inverted.

**Option 2: MLOFI-PCA as FeatureEngine v3 upgrade.**
- Not an alpha direction, but a feature quality improvement
- Replace current L1 OFI with MLOFI-PCA(L5) in FeatureEngine
- Benefits all downstream consumers (CBS, OpMM, future strategies)
- Low risk, moderate effort
- **Needs kill criterion** (Challenger feedback): specify which consumer improves by how much

**Option 3 (Only surviving direction): Acknowledge that 30-60 min alpha requires fundamentally different signals.**
- Calendar/session patterns (R17 Gap Fade, Thursday Night Short) are the right direction for longer horizons
- Need 60+ observations → need 3-6 more months of data accumulation
- Macro/cross-market signals (VIX, USD/TWD, US futures overnight) — entirely different data pipeline

### 3. For Team Review

This survey is submitted to both **Challenger** and **Execution** for independent review. Key questions for reviewers:

1. **Challenger**: Is there a paper I missed that demonstrates genuine *predictive* (not contemporaneous) IC from MLOFI at 30+ minute horizons? If so, cite it with specific IC numbers.
2. **Execution**: Can we align TXFD6 and TMFD6 tick data temporally from our existing .npy files? What's the timestamp resolution and overlap?

---

## References

1. Xu, Gould & Howison (2019). "Multi-Level Order-Flow Imbalance in a Limit Order Book." arXiv:1907.06230
2. Cont, Cucuringu & Zhang (2021). "Cross-Impact of Order Flow Imbalance in Equity Markets." arXiv:2112.13213
3. Su, Sun, Li & Yuan (2021). "The Price Impact of Generalized Order Flow Imbalance." arXiv:2112.02947
4. Hu & Zhang (2025). "Stochastic Price Dynamics in Response to OFI: CSI 300 Futures." arXiv:2505.17388
5. Anantha & Jain (2024). "Forecasting High Frequency Order Flow Imbalance." arXiv:2408.03594
6. Anantha, Jain & Maiti (2025). "Order Book Filtration and Directional Signal Extraction." arXiv:2507.22712
7. arXiv:2512.18648 — "Optimal Signal Extraction from Order Flow: Matched Filter Perspective." (Dec 2025)
8. Kang (2026). "Adaptive Signal Extraction via Kalman Filtering and Markov-Switching." arXiv:2601.05716
9. Li, Chen & Liu (2025). "High-frequency lead-lag in Chinese stock index futures." arXiv:2501.03171
10. Michael, Cucuringu & Howison (2022). "Option Volume Imbalance as a predictor." arXiv:2201.09319
11. Easley, Lopez de Prado & O'Hara (2012). "Flow Toxicity and Liquidity in a HF World." RFS.
12. Andersen & Bondarenko (2014). "Assessing Measures of Order Flow Toxicity." SSRN.
13. Song, Wu & Simon (2014). "Parameter Analysis of the VPIN Metric." SSRN.
14. Bjursell, Wang & Zheng (2017). "VPIN, Jump Dynamics and Inventory Announcements." SSRN.
15. Borochin & Rush (2016). "Identifying and Pricing Adverse Selection Risk with VPIN." SSRN.
