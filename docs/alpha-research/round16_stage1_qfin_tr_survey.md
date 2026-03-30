# Round 16 Stage 1: q-fin.TR Literature Survey

**Date**: 2026-03-26
**Direction**: arXiv q-fin.TR (Trading and Market Microstructure)
**Scope**: 50+ papers surveyed across 6 search queries, 3 papers deep-read

## Platform Constraints (Non-Negotiable)

| Constraint | Value | Source |
|---|---|---|
| Round-trip cost | 2.3 bps (comm 0.3 + tax 2.0) | feedback_taifex_fee_structure.md |
| Maker rebate | **None** | retail account |
| Broker RTT (P95) | 36ms submit, 43ms modify, 47ms cancel | latency_profiles.yaml |
| Data available | 22 trading days | research/data/raw/ |
| Directional ceiling | ~0.001 bps capturable | Round 14 empirical |
| OpMM edge | +0.32 bps/RT at spread > 2.5 bps | Round 13 |
| Tick interval | 125ms median (TXFD6) | Round 13 |

## Papers Deep-Read

### P1: "The Market Maker's Dilemma" (Albers et al., 2502.18625)
- **Data**: Binance BTC perpetual, live trading experiment, 232K orders
- **Core finding**: Fill probability and post-fill returns are negatively correlated. Viable maker strategies must be **contrarian** (counter-trade prevailing OBI).
- **"Reversals"**: Cases where OBI falsely predicts next price move (~15% of high-fill-prob orders). Logistic regression with 173 features achieves SR +11.97 with balanced inventory strategy (+0.71 bp/RT net of rebates).
- **Top predictive features**: Return autocovariance (5s windows), trade intensity (30s), near-side liquidity, top-of-book survival time, recent amplitude
- **Limitation**: Requires 0.5 bp maker rebate. Strategy returns +0.71-1.21 bp/RT — **below our 2.3 bps cost floor**.
- **Transferable**: Feature design and reversal concept applicable to OpMM adverse selection filtering.

### P2: "ClusterLOB" (Zhang et al., 2504.20349)
- **Data**: NASDAQ MBO data, 15 stocks, full 2021
- **Core finding**: K-means++ clustering of individual orders into directional/opportunistic/MM types. Cluster-decomposed OFI outperforms aggregate OFI (SR 1.34-1.55 vs 0.60).
- **Limitation**: Requires MBO (market-by-order) data unavailable from Shioaji/TAIFEX. Zero transaction cost in evaluation. 1 year training data (we have 22 days).
- **Transferable**: Concept of OFI decomposition by behavioral proxy (not directly implementable).

### P3: "Returns and Order Flow Imbalances" (Takahashi, 2508.06788)
- **Data**: S&P 500 E-mini futures, 1-second frequency, structural VAR
- **Core finding**: Price impact `br` is dominated by **inverse depth** (coefficient +0.553, R2=54%). Depth-normalized OFI is theoretically and empirically superior. OFI shocks dissipate within 1 second. Macro announcements spike `br` (price impact) while suppressing `bf` (flow feedback).
- **Actionable**: `depth_normalized_ofi = ofi_l1 / avg_best_depth` — one arithmetic operation, strong theoretical backing (Cont et al. 1/2D prediction).
- **Also actionable**: Regime flag around scheduled announcements (FOMC, TWSE settlement) to modulate OFI signal weight.

### Supporting Papers (Abstract-Level)

| Paper | Key Idea | Relevance |
|---|---|---|
| Hu & Zhang 2505.17388 | OFI as OU process on CSI-300 futures, regime-dependent memory | Confirms horizon-dependent OFI heterogeneity |
| Lokin & Yu 2403.02572 | Fill probability model f(queue_near, queue_opp) | Execution optimization for OpMM |
| Ma et al. 2504.00846 | Latency effect on optimal execution | Accounts for 36ms RTT in fill model |
| Kang 2512.18648 | Matched filter OFI normalization (Korean market) | Trader-type normalization for OFI |
| Jafree et al. 2510.27334 | RL MM adverse-selects meta-order execution | Confirms AS is structural, not avoidable |

---

## Candidate Alpha Directions (3)

### Candidate A: Depth-Normalized OFI Signal Enhancement
**Paper basis**: Takahashi 2508.06788
**Mechanism**: Replace raw `ofi_l1` with `ofi_l1 / avg_best_depth` (or `ofi_l1 * inverse_depth`). Price impact is 55% explained by depth. Adding announcement regime flag.
**Implementation**: 1-2 new features in FeatureEngine + strategy config update. ~1-2 days.
**Expected impact**: Marginal improvement to OFI-based decision quality in OpMM. Better calibration of when OFI signals are informative (thin book = high impact) vs. noise (thick book).
**Data requirement**: Works with existing 22 days. Feature is structural, not statistical.
**Risk**: LOW. Minimal code change, strong theoretical backing, may not produce sufficient edge alone to cross 2.3 bps threshold.
**Novelty vs prior rounds**: Prior rounds used raw OFI. Depth normalization never tested.

### Candidate B: Reversal-Conditioned OpMM (Adverse Fill Filter)
**Paper basis**: Albers et al. 2502.18625
**Mechanism**: Enhance OpportunisticMM with a logistic regression filter that predicts "reversal" probability — the chance that imbalance is a false signal and post-fill return will be favorable. Key features:
1. Return autocovariance (5s, 30s windows) — oscillating price = reversal likely
2. Trade intensity / arrival rate (30s) — high intensity = reversal likely
3. Top-of-book survival time — short survival = reversal likely
4. Near-side queue depth — small = high fill prob (required for reversal candidate)
5. Recent amplitude (100ms, 1s) — sudden drop-then-recovery pattern

**Implementation**: 3-5 new features + logistic filter in OpMM. ~3-5 days.
**Expected impact**: Filter out ~50% of adverse OpMM fills. If current OpMM edge = +0.32 bps/RT, doubling it by eliminating the worst fills could yield +0.6-0.8 bps/RT. Still below 2.3 bps, but combined with Candidate A depth conditioning, may approach viability.
**Data requirement**: 22 days should suffice for logistic regression with 5-8 features. Cross-validation critical.
**Risk**: MEDIUM. Needs trade-side classification (buy/sell aggressor) which Shioaji may not provide directly. Return autocovariance computable from tick prices. TOB survival computable from BidAsk events.
**Novelty**: This IS the paper-grounded version of Round 14 C3 (Adverse Selection Filter). C3 was data-blocked (needed 2000+ fills); this approach uses market features instead of fill history, so it's testable immediately.

### Candidate C: Fill Probability-Aware Latency Execution Model
**Paper basis**: Lokin & Yu 2403.02572, Ma et al. 2504.00846
**Mechanism**: Model fill probability as f(queue_near, queue_opp, spread, latency) and optimize order placement for OpMM:
1. Predict P(fill) at current best price vs. P(fill) at 1-tick better price
2. Account for 36ms RTT: by the time our order arrives, queue may have changed
3. Optimal price = argmax(E[return | fill] * P(fill)) accounting for latency
4. Can also optimize cancel decisions: cancel only when P(adverse fill) > threshold

**Implementation**: Analytical model (semi-closed-form from Lokin & Yu) + integration with OpMM order logic. ~5-7 days.
**Expected impact**: Better fill quality — fewer fills at adverse prices, more fills at favorable prices. Reduces effective spread cost. Improvement estimate: 0.2-0.5 bps per RT if current fill quality is suboptimal.
**Data requirement**: 22 days sufficient — model is structural (queuing theory), calibrated from a few parameters.
**Risk**: MEDIUM-LOW. Well-understood analytical framework. TXFD6 queue dynamics may differ from FX/US futures. Needs empirical queue calibration.
**Novelty**: Platform has no fill probability model. New execution layer.

---

## Researcher Recommendation

**Priority order**: B > A > C

**Rationale**:
- **B (Reversal OpMM)** has the highest expected edge amplification and directly addresses our #1 problem (adverse selection in OpMM). It's the paper-grounded successor to C3 with immediate testability.
- **A (Depth-Normalized OFI)** is near-zero cost/risk and should be implemented regardless. Can be bundled with B.
- **C (Fill Probability)** is complementary but requires more engineering and the improvement is in execution quality (0.2-0.5 bps), not signal quality.

**Combined scenario**: If A+B together yield +0.8-1.0 bps/RT and C adds +0.3 bps execution improvement, total = 1.1-1.3 bps/RT. Still below 2.3 bps cost floor. **None of these candidates alone or combined are expected to produce a profitable standalone strategy.**

**Honest assessment**: The q-fin.TR literature confirms what Round 14 discovered empirically — for retail TAIFEX participants without maker rebates, the cost structure is the binding constraint, not the signal quality. These candidates are best viewed as **OpMM enhancement features** that improve an already marginally positive strategy, not as new alpha sources.

---

## Stage 2 Implementation (2026-03-26)

**Selected**: A (Depth-Normalized OFI) + B (Reversal-Conditioned OpMM)
**Rejected**: C (Fill Probability Model) — Challenger FAIL + Execution defer

### Changes Implemented

**FeatureEngine v2 (`lob_shared_v2`, schema_version=2, 19 features)**:
- `[16] ofi_depth_norm_ppm`: OFI EMA8 / avg L1 depth * 1M. Captures depth-dependent price impact.
- `[17] ret_autocov_5s_x1e6`: Lag-1 return autocovariance over 40-tick rolling window. Negative = oscillating (reversal).
- `[18] tob_survival_ms`: Milliseconds since last best price change. Short = volatile TOB.

**Files modified**:
- `src/hft_platform/feature/registry.py` — Added `lob_shared_v2` FeatureSet, set as default
- `src/hft_platform/feature/engine.py` — Added `_LobKernelState` v2 fields + `_compute_v2_features()`
- `src/hft_platform/strategies/opportunistic_mm.py` — Added reversal filter (configurable via params)
- `tests/unit/test_feature_engine.py` — 10 new v2 tests
- `tests/unit/test_opportunistic_mm.py` — 10 new reversal filter tests
- `tests/unit/test_feature_engine_coverage.py` — Updated for v2 defaults
- `tests/unit/test_feature_backtest_adapter.py` — Updated for v2 tuple length

**Backward compatibility**: v1 (`lob_shared_v1`) still registered and selectable via `feature_set_id="lob_shared_v1"`.

### Reversal Filter Configuration

```python
OpportunisticMM(
    reversal_filter_enabled=True,      # Enable v2 filter
    reversal_autocov_threshold=0,       # Only quote when autocov < 0 (oscillating)
    reversal_tob_max_ms=2000,          # Only quote when TOB volatile (< 2s)
    reversal_min_depth_ratio=0.3,      # Skip when depth extremely imbalanced
)
```

### Awaiting

- Stage 3: Data preparation for empirical validation
- Stage 4: Backtest with latency + cost modeling
- Stage 5: Statistical validation (IC, DSR)
