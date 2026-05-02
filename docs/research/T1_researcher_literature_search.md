# T1: Literature Search — Optimal Execution & TCA

## Gaps Identified

1. **Fill Probability Model**: `ExecutionOptimizer.decide()` uses ad-hoc `Q_opp / Q_near > 1.5`. No theoretical basis.
2. **TCA Decomposition**: `market_impact_bps = 0.0` placeholder. `TCAAnalyzer.daily_report()` returns zeros for 5/9 metrics.
3. **Latency-Aware Execution**: `RegimeClassifier` not quantitatively connected to `ExecutionOptimizer`.

## Candidate 1: Cont-Kukanov Fill Probability Model

**Papers**: Cont & Kukanov (2012) arXiv:1210.1625, Lokin & Yu (2024) arXiv:2403.02572, Ma et al. (2025) arXiv:2504.00846

**Problem**: Replace ad-hoc fill_score threshold with principled fill probability.

**Approach**: Cont-Kukanov Proposition 3 — optimal limit/market split for single exchange. Inputs: queue depth Q, target qty S, order flow distribution F(x) from CK LOB data. Lokin-Yu extends to state-dependent flows using birth-death queuing models.

**Expected improvement**: 2-5 bps on US equities (paper). For TXFD6: ~10 NTD/contract on wrong-type orders. Value highest during volatility bursts (when spread widens and R47 trades most).

**R47 compatibility**: HIGH — slots into execution layer only, doesn't change alpha signal.

**Implementation**: Medium. Modify `ExecutionOptimizer.decide()`, add `execution/fill_model.py`, calibration script.

**Risk**: TXFD6 spread=1pt most of the time — binary limit/market decision. Value concentrated in wide-spread moments.

## Candidate 2: Implementation Shortfall TCA with Per-Fill Decomposition

**Papers**: Markov (2019) arXiv:1904.01566, Labadie & Lehalle (2012) arXiv:1205.3482, Busseti & Lillo (2012) arXiv:1206.0682

**Problem**: TCAAnalyzer returns zeros for delay_cost, exec_cost, impact, p95. No post-trade analysis capability.

**Approach**: Standard IS decomposition: delay_cost, execution_cost already structurally present but not aggregated. Add transient impact model (simplified for single-lot). Replace SUM/COUNT query with per-fill ClickHouse SQL using quantile(0.95)().

**Expected improvement**: Observability (not direct PnL). Enables: (a) detect execution degradation, (b) measurement for validating C1/C3, (c) meaningful daily Telegram TCA report.

**R47 compatibility**: PERFECT — pure measurement, changes no trading logic.

**Implementation**: Low. ~20 lines `tca/slippage.py`, ~40 lines `tca/analyzer.py`, calibration script.

**Risk**: market_impact for single-lot TXFD6 may be ~0 (noise). Delay/execution cost are the useful components.

## Candidate 3: Latency-Adjusted Limit Order Placement with Adverse Selection Control

**Papers**: Lehalle & Mounjid (2016) arXiv:1610.00261, Ma et al. (2025) arXiv:2504.00846, Gueant et al. (2011) arXiv:1105.3115

**Problem**: RegimeClassifier → ExecutionOptimizer connection is binary gate. No quantitative latency impact model.

**Approach**: Lehalle-Mounjid: LOB imbalance predicts fill direction (we already compute imbalance features). Quantify adverse selection cost as function of imbalance + latency. Cancel/re-insert value depends on Shioaji RTT.

**Expected improvement**: 0.5-2 bps vs naive limit orders. May conclude 30-50ms RTT is too high for active monitoring — useful knowledge.

**R47 compatibility**: HIGH with care — R47's queue position is its edge. Active cancel/reinsert must preserve queue priority.

**Implementation**: Medium-High. Extend `ExecutionOptimizer`, connect `RegimeClassifier` quantitatively, add `execution/latency_model.py`.

**Risk**: 30-50ms RTT may be above threshold where monitoring adds value. Risk to R47 queue priority.

## Pre-Research Gate

| Gate | C1 (Fill Model) | C2 (TCA Decomp) | C3 (Latency) |
|------|-----------------|------------------|---------------|
| Q1 Measurable? | YES | YES | YES |
| Q2 R47 compatible? | YES | YES | YES (with care) |
| Q3 Implementable? | YES | YES | YES |

## Recommended Priority

1. **C2 first** — Low risk, provides measurement infrastructure
2. **C1 second** — Medium effort, directly improves execution
3. **C3 third or PARK** — Highest complexity, may not be viable at our latency
