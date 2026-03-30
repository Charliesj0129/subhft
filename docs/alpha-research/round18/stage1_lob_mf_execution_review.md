# R18 Stage 1 Execution Review: LOB-Driven Medium-Frequency Survey

**Reviewer**: Execution Reviewer
**Date**: 2026-03-26
**Survey**: `docs/alpha-research/round18_stage1_lob_mf_survey.md`

---

## Reference Parameters

| Parameter | Value | Source |
|-----------|-------|--------|
| Shioaji P95 submit RTT | 36 ms | `config/research/latency_profiles.yaml` |
| Shioaji P95 modify RTT | 43 ms | same |
| Shioaji P95 cancel RTT | 47 ms | same |
| Internal pipeline latency | ~250 us | same |
| TXFD6 RT cost | 39.2 NTD (3.92 pts, ~1.19 bps) | established R16 |
| TMFD6 RT cost | ~4 pts (~1.33 bps) | established R16 |
| FeatureEngine version | `lob_shared_v2` (21 features, indices 0-20) | `feature/registry.py` |
| IC breakeven at 30 min | 0.043 | established R17 |
| IC breakeven at 60 min | 0.030 | established R17 |
| TXFD6 median tick interval | 125 ms | established R13 |

---

## Candidate A: Generalized Stationarized OFI (log-GOFI)

### Verdict: CONDITIONAL APPROVE (top priority)

**1. Latency profile vs signal half-life**
- Holding period: 5-15 minutes.
- At 36ms P95 RTT, signal lifetime of 5 minutes means latency consumes 0.012% of signal duration. Latency is completely irrelevant for this horizon.
- Signal is computed over rolling 5-minute windows -- slowly varying, highly latency-robust.
- **Assessment: STRONG PASS**

**2. Data availability**
- Required: Multi-level LOB snapshots (price + volume per level).
- We have: 5-level LOB via `BidAskEvent` with `bids/asks: np.ndarray` shape (N,2). This is BETTER than the paper's 3-second snapshots on CSI 500 -- we have tick-by-tick L5 data.
- The computation requires tracking which price levels the BBO traversed between updates. This state is NOT currently maintained in the pipeline but is derivable from consecutive `BidAskEvent` snapshots.
- Historical data in ClickHouse: 9.16M rows for TMFD6, sufficient for backtesting.
- **Assessment: PASS -- data exists, needs new processing logic**

**3. Feature engine compatibility**
- log-GOFI is NOT in `lob_shared_v2`. Current OFI features (`ofi_l1_raw` [11], `ofi_l1_cum` [12], `ofi_l1_ema8` [13], `ofi_depth_norm_ppm` [16]) are all L1-only.
- log-GOFI fundamentally differs: it aggregates across ALL price levels the BBO traversed, not just the current best level. This is a multi-level computation requiring:
  - Previous BBO state tracking
  - Identification of traversed levels
  - Depth aggregation across those levels
  - Log stationarization: `log(1 + q)` per level
- This does NOT fit as a simple FeatureEngine feature (current features are stateless or single-EMA rolling). It would need either:
  - (a) A new multi-event stateful feature in FeatureEngine (MEDIUM complexity, ~80-120 LOC)
  - (b) Strategy-internal computation from raw BidAskEvent (violates feature centralization but acceptable for research prototype)
- **Assessment: CONDITIONAL -- needs new feature or prototype-scoped exception**

**4. Cost model**
- TXFD6: 3.92 pts RT cost. At 5-15 min horizon, typical TXFD6 price moves are 10-40 pts. The signal needs IC > 0.043 at 30 min to break even. The paper reports R-squared of 83-86% CONTEMPORANEOUS. Contemporaneous R-squared does NOT equal predictive IC -- this is the critical risk.
- R16 finding: OFI signals on TMFD6 work 5-15s, die at 60s+. If log-GOFI's multi-level aggregation and stationarization create genuine persistence beyond standard OFI, the cost model works. If not, this fails the same way R9-R16 OFI signals did.
- The survey correctly flags "contemporaneous vs predictive" as Key Risk #1.
- **Assessment: UNKNOWN until predictive IC is measured. Cost model is viable IF signal persists.**

**5. Platform integration path**
- MEDIUM complexity for research prototype:
  - Process consecutive BidAskEvent pairs to identify traversed levels
  - Aggregate depth changes with log stationarization
  - Rolling 5-minute accumulator
  - ~100-150 LOC for research prototype
- For production: would need new FeatureEngine feature slot(s), potentially [21+].
- Key advantage: uses existing L5 BidAskEvent data pipeline. No new data sources needed.
- **Assessment: MEDIUM effort, well-scoped**

### Key Execution Concern for A
The paper's 83.6% R-squared is **contemporaneous** (same-window). Our R9-R16 history shows OFI signals have high contemporaneous explanatory power but weak-to-zero predictive power beyond 15 seconds on Taiwan futures. The ENTIRE value proposition of log-GOFI rests on whether multi-level aggregation + log stationarization create predictive persistence that standard L1 OFI lacks. Stage 2 must measure **predictive** IC at 5/10/30 min horizons with STRICT out-of-sample protocol. If predictive IC < 0.043 at 30 min, kill immediately.

---

## Candidate B: OFI Regime-Switching with Optimal Horizon (OFI-OU)

### Verdict: CONDITIONAL APPROVE (second priority)

**1. Latency profile vs signal half-life**
- Holding period: 5-30 minutes. Same analysis as Candidate A.
- Regime detection adds a second timescale: the regime lookback window. If regime changes are slow (minutes to hours), detection latency is irrelevant. If fast (seconds), detection noise is the risk, not order latency.
- **Assessment: STRONG PASS**

**2. Data availability**
- Required: LOB for OFI computation + OU parameter fitting + regime detection.
- We have: OFI already in FeatureEngine v2 (`ofi_l1_raw` [11], `ofi_l1_ema8` [13]). OU fitting requires rolling return series (computable from `mid_price_x2` [2]). Regime detection via rolling autocorrelation of OFI -- all inputs available.
- **Assessment: PASS -- all inputs available from existing features**

**3. Feature engine compatibility**
- Core OFI features exist. New components needed:
  - OU parameter estimator (mean-reversion speed kappa, long-run mean, volatility). This is a rolling regression/MLE problem -- NOT a tick-level feature. Better as strategy-level computation.
  - Quasi-Sharpe ratio calculator: `E[return|OFI shock] / std[return|OFI shock]` as function of horizon. This is a research metric, not a real-time feature.
  - Regime detector: rolling autocorrelation of OFI. Could be a new FE feature (`ofi_autocorr_Ns`) or strategy-internal.
- The paper's innovation is analytical framework (quasi-Sharpe for horizon selection, OU for dynamics), not a new real-time signal. The tradable signal is still "OFI in low-efficiency regime" -- which is a conditional version of existing OFI.
- **Assessment: PARTIAL PASS -- OFI exists; regime/horizon framework is research-level, not FE-level**

**4. Cost model**
- Same cost structure as Candidate A. At 5-30 min horizons, TXFD6 moves 10-60 pts. Cost model works if signal persists.
- Paper reports OFI correlation >0.50 from 5s to 60min on CSI 300. This is extraordinary persistence.
- **CRITICAL FLAG**: Our R16 found OFI signals die at 60s+ on TMFD6. The paper's CSI 300 result may not transfer. CSI 300 Index Futures are extremely liquid (tight spreads, deep books, institutional-dominated). TMFD6 is retail-dominated, wider spreads, thinner books. The OU mean-reversion speed is likely MUCH faster on TMFD6, killing persistence.
- Survey correctly flags this as Key Risk #2.
- **Assessment: HIGH RISK of non-transfer. Must measure OFI autocorrelation on TXFD6/TMFD6 FIRST.**

**5. Platform integration path**
- LOW complexity for research prototype:
  - Rolling OFI autocorrelation (existing OFI feature + standard stats)
  - OU parameter fitting (scipy.optimize or analytical MLE)
  - Quasi-Sharpe computation at multiple horizons
  - ~80-120 LOC
- For production: regime gate as a strategy-level filter (similar to CBS session gate). No FE changes needed.
- **Assessment: LOW effort**

### Key Execution Concern for B
The paper's finding of OFI correlation >0.50 at 60-minute horizon on CSI 300 directly contradicts our R16 finding that OFI dies at 60s+ on TMFD6. This discrepancy is likely instrument-specific (liquidity, participant mix). Before ANY prototyping, measure OFI return-correlation at 30s, 1min, 5min, 10min, 30min on TXFD6 and TMFD6. If correlation drops below 0.10 by 5 minutes, kill this candidate -- the OU/regime framework adds no value if the underlying signal doesn't persist.

**Suggested protocol**: This measurement can be done in 2-4 hours with existing ClickHouse data. Recommend doing it BEFORE Stage 2 prototyping as a gate-zero check.

---

## Candidate C: ClusterLOB -- Participant-Decomposed OFI

### Verdict: REJECT (data gap too severe)

**1. Latency profile vs signal half-life**
- Holding period: 30 minutes. Latency completely irrelevant.
- **Assessment: PASS**

**2. Data availability**
- Required: Market-by-order (MBO) data with individual order IDs, types, lifetimes.
- **WE DO NOT HAVE MBO DATA.** Our data pipeline provides LOB snapshots (aggregate state), not individual order messages. Shioaji does not provide order-level data for futures.
- The survey acknowledges this as a "CRITICAL GAP" and proposes a workaround: classify LOB state changes as "aggressive" vs "passive." This is a significant methodological deviation that removes the paper's core innovation (participant clustering on order features).
- **Assessment: FAIL -- fundamental data gap, workaround is a different strategy**

**3. Feature engine compatibility**
- N/A given data gap. Even the workaround (aggressive/passive classification from LOB deltas) would need new features that approximate something we cannot validate against ground truth.
- **Assessment: FAIL**

**4. Cost model**
- 30-minute holding is cost-friendly. The paper does not report absolute PnL after costs, only relative improvement over undifferentiated OFI. Cannot assess whether the edge covers our cost structure.
- **Assessment: UNKNOWN**

**5. Platform integration path**
- The workaround (heuristic aggressive/passive decomposition from LOB state changes) is implementable but:
  - No way to validate the heuristic without MBO ground truth
  - "Aggressive" vs "passive" classification from snapshot deltas is noisy and regime-dependent
  - Results would be uninterpretable: if it fails, is the heuristic wrong or the strategy wrong?
- **Assessment: HIGH RISK, unvalidatable**

### Recommendation for C
Kill this candidate. The MBO data gap is not a minor limitation -- it removes the paper's core mechanism (K-means clustering on 6 order-level features). The proposed workaround is a different, untested heuristic with no validation path. Pursuing it would burn Stage 2 cycles on an unvalidatable approximation.

If MBO data becomes available (e.g., from TAIFEX direct feed), this candidate could be revisited in a future round.

---

## Summary Table

| Candidate | Verdict | Latency | Data | Features | Cost | Integration | Priority |
|-----------|---------|---------|------|----------|------|-------------|----------|
| A: log-GOFI | APPROVE | STRONG PASS | PASS | CONDITIONAL (new feature) | UNKNOWN (predictive IC?) | MEDIUM | **1st** |
| B: OFI-OU Regime | APPROVE | STRONG PASS | PASS | PARTIAL (OFI exists, regime new) | HIGH RISK (non-transfer) | LOW | **2nd** |
| C: ClusterLOB | REJECT | PASS | FAIL | FAIL | UNKNOWN | HIGH RISK | Kill |

---

## Cross-Cutting Execution Concerns

### 1. Contemporaneous vs Predictive (Applies to A and B)
Both papers report contemporaneous R-squared or correlation, not predictive IC. Our R9-R16 history shows OFI has high contemporaneous explanatory power but fails predictively on Taiwan futures beyond 15 seconds. This is the single largest risk for this round. **Mandatory gate-zero**: measure predictive IC at 5/10/30 min before any strategy prototyping.

### 2. CSI 300/500 vs TXFD6/TMFD6 Transfer Risk
Both papers validated on Chinese index futures (CSI 300, CSI 500). These are among the world's most liquid futures contracts. TXFD6 and especially TMFD6 have:
- Lower liquidity (thinner books)
- Wider spreads (especially TMFD6)
- Different participant mix (more retail)
- Different tick structure

Signals validated on CSI may not transfer. This is not a reason to reject, but Stage 2 must measure on OUR data first, not assume transferability.

### 3. OFI Fatigue Risk
Rounds 9-12, 15-16 all tested OFI variants. log-GOFI and OFI-OU are novel constructions, but the base signal is still order flow imbalance. If the fundamental issue is that OFI doesn't persist beyond 15 seconds on Taiwan futures (R16 finding), no amount of stationarization or regime detection will fix it. The gate-zero OFI persistence measurement is CRITICAL.

### 4. Existing Feature Overlap
`ofi_depth_norm_ppm` [16] in FE v2 already normalizes OFI by depth (Takahashi-inspired). log-GOFI's multi-level aggregation is structurally different, but incremental IC over `ofi_depth_norm_ppm` must be measured to justify the added complexity.

### 5. Config Drift Check

| Parameter | Survey Value | Platform Value | Drift? |
|-----------|-------------|---------------|--------|
| RT cost TXFD6 | ~39 NTD | 39.2 NTD | No (rounding) |
| RT cost TMFD6 | ~4 pts | 3.92 pts | No (rounding) |
| RTT P95 | ~36ms | 36ms | No |
| IC breakeven 30min | 0.043 | 0.043 (R17) | No |
| IC breakeven 60min | 0.030 | 0.030 (R17) | No |

No config drift detected.

---

## Mandatory Pre-Stage-2 Measurements (Gate Zero)

Before prototyping ANY candidate, the following empirical measurements must be completed on TXFD6 and TMFD6 ClickHouse data:

1. **OFI return-correlation at multiple horizons**: Measure correlation between `ofi_l1_raw` and forward returns at 30s, 1min, 5min, 10min, 30min. Kill gate: if correlation < 0.05 at 5min, kill both A and B.

2. **OFI autocorrelation decay profile**: Measure autocorrelation of `ofi_l1_raw` at lags 1s, 5s, 30s, 1min, 5min. If autocorrelation drops to zero by 60s (consistent with R16), the persistence premise for both A and B is invalid.

3. **Incremental value of L5 vs L1**: Compare `ofi_l1_raw` correlation with a rough multi-level OFI proxy computed from ClickHouse L5 data. If L5 adds < 10% incremental R-squared over L1, log-GOFI's multi-level innovation adds no value on our instrument.

Estimated effort: 2-4 hours with existing ClickHouse data. This investment prevents wasting 1-2 weeks on candidates that fail the same OFI persistence issue as R9-R16.

---

*Execution Reviewer -- R18 Stage 1 (LOB Medium-Frequency)*
