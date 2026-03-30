# R24 Stage 1: Literature Survey & Infrastructure Gap Analysis

**Date**: 2026-03-29
**Researcher**: Alpha Research Agent
**Direction**: Project Infrastructure & Research (專案infra與研究)

---

## Executive Summary

L1 microstructure alpha is **exhausted** (confirmed R14-R23). This survey identifies 3 candidate directions that leverage **infrastructure investments** to unlock new signal sources rather than squeezing more from existing LOB features. Each direction requires specific platform changes but builds on existing architecture.

---

## Candidate Direction A: Execution Quality Optimization via State-Dependent Fill Probability Modeling

### Description

Instead of finding new alpha signals (which R14-R23 proved exhausted at L1), optimize the **execution layer** to extract more PnL from existing signals. Current `ExecutionOptimizer` uses a simple heuristic (spread threshold + Q_opp/Q_near ratio). Replace it with a state-dependent fill probability model that learns from historical fill/cancel outcomes, adapting placement decisions to LOB microstate. This is the highest-ROI direction because it improves PnL for ALL strategies without new alpha.

### Paper References

1. **Cont & Kukanov (2012)** — "Optimal Order Placement in Limit Order Markets" (arXiv:1210.1625). Foundational framework: formulates order placement as convex optimization over (price, fill probability, fee) tradeoff. Directly applicable to our limit-vs-market decision.

2. **Lehalle & Neuman (2024)** — "Fill Probabilities in a Limit Order Book with State-Dependent Stochastic Order Flows" (arXiv:2403.02572). Semi-analytical fill probability as function of queue position, order flow intensity, and book depth. Models state-dependent arrival rates (Hawkes-style). **Key insight**: fill probability is NOT just queue depth — it depends on order flow regime.

3. **Fang et al. (2023)** — "Tackling the Problem of State Dependent Execution Probability: Empirical Evidence and Order Placement" (arXiv:2307.04863). ML-based fill probability using microstructural features (spread, imbalance, depth, trade intensity). Neural network infers fill probability function for fixed horizon. **Directly applicable** — our FeatureEngine v3 already computes all required inputs.

4. **Coletta et al. (2025)** — "Right Place, Right Time: Market Simulation-based RL for Execution Optimisation" (arXiv:2510.22206). RL agent learns execution timing from LOB simulator. Interpretable policy output. Relevant for timeout/cancel decision.

5. **Cont et al. (2025)** — "Stochastic Price Dynamics in Response to Order Flow Imbalance: Evidence from CSI 300 Index Futures" (arXiv:2505.17388). OFI-driven price model with OU mean-reversion. Relevant for modeling adverse selection risk during limit order waiting period.

### Infrastructure Prerequisites

- **Data**: Record fill/cancel outcomes per order with LOB microstate snapshot at decision time. **Already partially supported**: `hft.orders` + `hft.trades` tables exist, `hft.market_data` has L1+L5 depth. Need: join fill events with LOB state at order-placement time (new `hft.execution_decisions` table or enriched fill records — ~50 LOC migration + recorder change).
- **Feature**: All 27 v3 features already available. Need: expose feature snapshot at order-placement time to execution layer (wire `FeatureEngine.get_latest()` into `ExecutionOptimizer.decide()` — ~30 LOC).
- **Model**: Logistic regression or shallow NN trained on (features, spread, depth, side) -> P(fill | horizon). Offline training, online inference. **No new data pipeline needed**.
- **Backtest**: Extend existing backtest framework to replay execution decisions against recorded LOB states. Moderate effort (~200 LOC).

### Estimated Signal Horizon & Expected IC Range

- **Horizon**: Per-order decision (not time-series alpha). Measured as cost savings in bps/trade.
- **Expected improvement**: 0.5-2.0 bps/trade cost reduction. At current TMFD6 RT cost of 3.92 pts (1.19 bps), even 0.5 bps improvement is material (42% cost reduction when applicable).
- **Benchmark**: Albers 2025 reports 1.2 pts/trade savings from passive placement on TXFD6 (confirmed R16). Our current heuristic captures some of this; ML model should capture more.

### Kill Gate Criteria

1. **OOS fill prediction AUC < 0.60** on held-out month — model adds no value over heuristic.
2. **Net execution cost improvement < 0.3 bps/trade** on OOS replay — not worth complexity.
3. **Latency overhead > 100us per decision** — violates hot-path performance budget.

### Risk Assessment: Overlap with Killed Directions

- **NO overlap**. This is execution optimization, not alpha research. Does not revisit any R14-R23 killed signal.
- **Risk**: Model overfits to specific spread regime (Jan/Feb wide spread vs March narrow). Mitigation: train on multi-month data, test on most recent month first (feedback: recency bias guard).

---

## Candidate Direction B: Cross-Instrument Options Flow Pipeline (TXO -> TXFD6/TMFD6)

### Description

Build the infrastructure to ingest, normalize, and compute features from **Taiwan options (TXO) trade-level data**. R17 identified TXO as having 115K ticks (quotes only, not trades) — the data pipeline gap is the blocker, not the signal. Options flow carries informed trading signal that leads futures by seconds to minutes. Once the pipeline exists, extract put-call volume imbalance, delta-hedging flow estimation, and options OFI as new feature inputs.

### Paper References

1. **Michael (2022)** — Referenced in R17. Options volume imbalance as informed trading proxy. Signal proven overnight, untested intraday due to data gap.

2. **Lehalle et al. (2025)** — "Inferring Latent Market Forces: Evaluating LLM" (arXiv:2512.17923). Dealer hedging pattern detection achieving 71.5% accuracy. Gamma positioning and delta hedging signals. **Key insight**: options market makers' hedging creates predictable futures order flow.

3. **arXiv:2512.12924** — "Interpretable Hypothesis-Driven Trading" (December 2025). Develops directional option-to-stock trading-volume imbalances, demonstrating prediction of future abnormal returns.

4. **arXiv:2601.18804** — "Deep g-Pricing for CSI 300 Index Options with Volatility Trajectories and Market Sentiment" (January 2026). Options implied volatility surface dynamics carry predictive information for underlying futures.

5. **Hu & Zhang (2023)** — Referenced in R16. OFI regime detection. Cross-asset OFI (options + futures) would be genuinely new signal vs single-asset L1 OFI (exhausted).

### Infrastructure Prerequisites

- **Data Pipeline (CRITICAL GAP)**: Shioaji/Fubon API already supports TXO subscription. Need:
  1. Add TXO symbols to subscription config (~5 LOC in `symbols.yaml`).
  2. Instrument metadata already supported (migration `20260330_001`). `InstrumentRegistry` has `InstrumentType.OPTION`, `OptionRight`, `strike_scaled`, `expiry`.
  3. Normalizer already handles multi-instrument via `instrument_type` field.
  4. **New**: Options-specific feature engine module to compute put-call ratio, options OFI, implied vol proxy from trade prices. ~300-500 LOC new module.
  5. **New**: Cross-instrument feature aggregation (TXO signals -> TXFD6/TMFD6 strategy input). Need bus wiring to route options features to futures strategy. ~100 LOC.
- **Storage**: ClickHouse schema already supports options via `instrument_type`, `strike_scaled`, `option_right`, `expiry` columns. **No schema change needed**.
- **Data accumulation**: Need 20+ trading days of TXO tick data before any meaningful validation. ~4 weeks lead time.

### Estimated Signal Horizon & Expected IC Range

- **Horizon**: 30s-300s (options flow leads futures at MF horizon where our L1 signals are dead).
- **Expected IC**: 0.03-0.08 (cross-asset signals typically weaker but ORTHOGONAL to L1 features).
- **Critical uncertainty**: TXO data may still be mostly quotes (R17 finding: 115K rows, 99.7% quotes). If trade ticks are sparse, signal will be weak. This MUST be validated before committing to feature engineering.

### Kill Gate Criteria

1. **TXO trade tick density < 100 trades/day** — insufficient granularity for intraday signal. Kill and revisit when TAIFEX provides better data.
2. **Cross-asset OFI detrended IC < 0.02 at 60s** — no information content above noise.
3. **Put-call ratio signal correlation with L1 depth_imbalance > 0.5** — not genuinely new signal, just a proxy.
4. **Data accumulation: 20 days minimum before any IC measurement**.

### Risk Assessment: Overlap with Killed Directions

- **Partial overlap with R17 OIDS**: R17 killed OIDS due to data quality (99.7% quotes, not trades). This direction explicitly addresses the data gap first. If TXO data quality is still bad after pipeline build, Direction B is killed.
- **NO overlap with killed L1 microstructure directions** (R15-R23). This is genuinely cross-asset.
- **Risk**: 4-week data accumulation delay. High infra cost (~500 LOC) for uncertain payoff.

---

## Candidate Direction C: Adaptive Execution Timing via Intraday Regime Detection

### Description

Combine the existing `BurstDetector`, `VRR` (variance ratio) feature, and `toxicity_ema50` into a **regime-aware execution timing layer** that gates order entry based on detected market state. R14 CBS already uses time-of-day gating; this extends it to data-driven regime detection. Key insight from R13/R16: the bottleneck is not signal quality but **when to trade** — avoiding adverse selection periods and targeting favorable microstructure windows.

### Paper References

1. **Christensen (2024)** — Intensity-based regime detection. Already implemented as `BurstDetector`. Need: integrate regime output into execution decisions.

2. **arXiv:2510.27334** — "Adverse Selection of Meta-Orders by RL-Based Market Making" (October 2025). Models how HF market makers adversely select medium-frequency order flow. **Key insight**: timing of entry determines adverse selection exposure more than signal quality.

3. **arXiv:2509.12456** — "RL-Based Market Making as Stochastic Control on Non-Stationary LOB Dynamics" (September 2025). Models non-stationarity of LOB dynamics. Regime shifts matter for execution.

4. **arXiv:2505.08180** — "Forecasting Intraday Volume in Equity Markets with ML" (May 2025). U-shaped intraday volume pattern decomposition. Volume regime predicts execution quality.

5. **Albers et al. (2502.18625)** — Already referenced in FeatureEngine v2. TOB survival, return autocovariance as regime indicators. Our `ret_autocov_5s_x1e6` [17] and `tob_survival_ms` [18] are underutilized.

### Infrastructure Prerequisites

- **Feature availability**: All needed features ALREADY EXIST in FeatureEngine v3:
  - `toxicity_ema50_x1000` [21] — informed flow detector
  - `ret_autocov_5s_x1e6` [17] — reversal regime indicator
  - `tob_survival_ms` [18] — LOB stability indicator
  - `spread_ema300s` [26] — long-term spread regime
  - `BurstDetector` — tick intensity regime
- **New code**: Regime classifier that combines existing features into discrete states (FAVORABLE / NEUTRAL / ADVERSE). ~150 LOC. Simple threshold-based or logistic regression.
- **Integration**: Wire regime signal into `ExecutionOptimizer` and strategy runner as execution gate. ~50 LOC.
- **Backtest**: Replay regime labels against historical fill quality to validate. Use existing `hft.trades` + `hft.market_data` join. ~100 LOC.
- **Total new code**: ~300 LOC. **Lowest infrastructure cost of all 3 directions**.

### Estimated Signal Horizon & Expected IC Range

- **Horizon**: Not an alpha signal — it's an execution filter. Measured as adverse selection avoidance.
- **Expected improvement**: 1-3 bps reduction in adverse fill rate. R23 toxicity Q5-Q1 = +3.5 pts adverse movement on TXFD6 — if we avoid trading in Q5 toxicity windows, that's direct PnL improvement.
- **Compound effect with Direction A**: Regime gating (C) + fill probability model (A) together could save 2-4 bps/trade.

### Kill Gate Criteria

1. **Regime labels show no separation in OOS fill quality** (FAVORABLE vs ADVERSE fill PnL difference < 1 bps) — features are not predictive of execution quality.
2. **Trade frequency drops > 50% from gating** — too restrictive, kills strategy capacity.
3. **Regime transitions too frequent (> 20/hour)** — noisy, not actionable.

### Risk Assessment: Overlap with Killed Directions

- **NO overlap**. This combines existing features in a new way (execution timing, not alpha signal).
- **Builds on proven assets**: toxicity (R23 validated), burst detector (deployed), VRR concept (R22).
- **Low risk**: Smallest code footprint, uses only existing data and features.
- **Potential synergy with CBS**: Could replace the hard-coded ToD gate with data-driven regime gate.

---

## Infrastructure Gap Summary

| Gap | Blocking Which Direction | Effort | Priority |
|-----|--------------------------|--------|----------|
| Fill/cancel outcome recording with LOB snapshot | A | ~80 LOC (migration + recorder) | P0 for A |
| Feature snapshot exposure to execution layer | A, C | ~30 LOC | P0 for A, C |
| TXO subscription + options feature module | B | ~500 LOC + 4 weeks data accumulation | P1 for B |
| Cross-instrument bus wiring | B | ~100 LOC | P1 for B |
| Regime classifier module | C | ~150 LOC | P0 for C |
| Execution replay backtest framework | A, C | ~200 LOC | P1 for validation |
| Feature Engine Rust production hardening (TODO 1.1) | All (latency) | Large, ongoing | P2 |
| Research factory expansion (TODO 2.4) | All (throughput) | Large, ongoing | P2 |

## Recommendation

**Priority order: C > A > B**

1. **Direction C (Regime-Aware Execution)** — Start immediately. Lowest cost (~300 LOC), uses existing features, no new data dependencies. Quick validation possible with current data.

2. **Direction A (Fill Probability Model)** — Start after C validates. Medium cost, needs execution outcome recording infrastructure. High expected ROI (directly reduces cost).

3. **Direction B (Options Flow Pipeline)** — Start data collection now (subscribe to TXO), but defer feature engineering until 20+ days accumulated. Highest uncertainty but highest potential ceiling if TXO trade data quality is adequate.

Directions A and C are **complementary** and can be developed in sequence within the same execution optimization theme. Direction B is independent and parallel.

---

## Data Sources Referenced

- Platform: FeatureEngine v3 registry (`src/hft_platform/feature/registry.py`) — 27 features across 3 schema versions
- Platform: ExecutionOptimizer (`src/hft_platform/execution/execution_optimizer.py`) — current heuristic baseline
- Platform: BurstDetector (`src/hft_platform/feature/burst_detector.py`) — tick intensity regime
- Platform: InstrumentRegistry (`src/hft_platform/core/instrument_registry.py`) — options metadata support
- Platform: ClickHouse migrations (`src/hft_platform/migrations/clickhouse/`) — schema capabilities
- Research data: TXFD6 (14 days L1), TMFD6 (22 days L1), 2330 (4 days L1) — no L5, no TXO
- Prior rounds: R13 (MM structural failure), R14 (CBS), R16 (TMFD6 exhaustion), R17 (TXO gap), R18 (detrended IC gate), R22 (VRR), R23 (toxicity)
