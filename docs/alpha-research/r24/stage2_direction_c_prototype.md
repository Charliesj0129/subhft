# R24 Stage 2: Direction C — Regime-Aware Execution Timing Prototype

**Date**: 2026-03-29
**Round**: R24 (Alpha Research)
**Direction**: C — Adaptive Execution Timing via Intraday Regime Detection

---

## Summary

Implemented a `RegimeClassifier` that classifies LOB microstate into FAVORABLE / NEUTRAL / ADVERSE execution regimes using FeatureEngine v2+ features. Wired into `ExecutionOptimizer.decide()` as an execution gate. Backtested on 33 days of TXFD6+TMFD6 data.

**Result**: Signal is strong (2.3-15.2 pts regime separation), but transition rate exceeds kill gate. Needs holdoff tuning or EMA smoothing before production deployment.

## Implementation

### New Module: `src/hft_platform/execution/regime_classifier.py` (~200 LOC)

**Inputs**:
- Feature tuple from `FeatureEngine.get_feature_tuple(symbol)` (indices [17], [18], [21], [26])
- BurstDetector state via `burst_active` boolean parameter
- Timestamp for holdoff debouncing

**Classification Logic**:
```
ADVERSE  = burst_active OR |toxicity| > 400 OR tob_survival < 50ms
FAVORABLE = tob_survival > 500ms AND |ret_autocov| < 500
NEUTRAL  = everything else
```

**Features Used** (ranked by Diagnostic 0a correlation strength):
1. `tob_survival_ms` [18]: rho=-0.211 with |fwd_30s|. Primary regime indicator.
2. `ret_autocov_5s_x1e6` [17]: rho=-0.121 with |fwd_30s|. Autocorrelation regime.
3. `toxicity_ema50_x1000` [21]: R23 validated (Q5-Q1=+3.5 pts). Optional — NOT primary driver (no trade-tick validation data available).
4. `spread_ema300s` [26]: rho=+0.047. Disabled by default (below kill gate, noisy).
5. `BurstDetector`: Standalone API. Burst = immediate ADVERSE.

**Architecture Compliance**:
- `__slots__` on all classes (Allocator Law)
- O(1) classify(), no heap allocation (Allocator Law)
- `structlog` for logging (Coding Style)
- Holdoff debouncing via `ts_ns` parameter (5s default, configurable)

### Integration: `src/hft_platform/execution/execution_optimizer.py` (~30 LOC changed)

Added `regime: Regime = Regime.NEUTRAL` parameter to `decide()`:
- **ADVERSE**: Force MARKET (skip heuristic entirely)
- **FAVORABLE**: Relax spread threshold by 1 pt, relax fill_score threshold by 0.5
- **NEUTRAL**: Original heuristic unchanged (backward compatible)

### Tests: 57 total (all passing)

- `tests/unit/test_regime_classifier.py`: 25 tests
  - Basic, ADVERSE, FAVORABLE, priority, transitions, holdoff debouncing, short tuple, status
- `tests/unit/test_execution_optimizer.py`: 32 tests (27 existing + 5 new regime tests)
  - ADVERSE forces MARKET, FAVORABLE relaxes thresholds, NEUTRAL unchanged

## Backtest Results

### Diagnostic 0a: Feature Correlation (33 days)

| Feature | Pooled rho(|fwd_30s|) | Kill Gate (>0.05) |
|---------|----------------------|-------------------|
| tob_survival_ms | -0.211 | **PASS** |
| ret_autocov_5s_x1e6 | -0.121 | **PASS** |
| spread_ema300s | +0.047 | MARGINAL |
| toxicity_proxy | NaN | NOT TESTABLE (no trade ticks in L1 data) |

### Diagnostic 0b: ExecutionOptimizer Baseline

**Direction A KILLED**: Improvement ceiling = 0.02 pts/trade (heuristic is near-optimal).
The regime (Jan/Feb wide vs March tight) is the bottleneck, not the fill model quality.

### Diagnostic 1: Regime Backtest (with 5s holdoff)

**TMFD6** (20 days):

| Kill Gate | Result | Verdict |
|-----------|--------|---------|
| KG1: separation > 1 pt | +2.31 pts | **PASS** |
| KG2: ADVERSE < 50% | 27.7% | **PASS** |
| KG3: transitions < 20/hr | 321.6/hr | **FAIL** |

**TXFD6** (13 days):

| Kill Gate | Result | Verdict |
|-----------|--------|---------|
| KG1: separation > 1 pt | +15.20 pts | **PASS** |
| KG2: ADVERSE < 50% | 17.3% | **PASS** |
| KG3: transitions < 20/hr | 199.1/hr | **FAIL** |

### Key Findings

1. **Regime separation is strong**: ADVERSE windows show 2.3 pts (TMFD6) to 15.2 pts (TXFD6) higher forward volatility than FAVORABLE.

2. **Transition rate too high**: Even with 5s holdoff (reduced 10x from raw 3500/hr), transitions remain ~200-300/hr. Root cause: `tob_survival_ms` resets to 0 on every price change, which happens at tick-level frequency.

3. **Regime bifurcation confirmed**: Jan/Feb data (wide spreads, >96% FAVORABLE on TXFD6) vs March data (tight spreads, >47% ADVERSE on TMFD6). This directly explains Diagnostic 0b's fill rate bifurcation (0% Jan/Feb vs 50% March).

4. **Cost model correction**: TMFD6 RT = 4.0 pts / 1.33 bps (corrected from 3.92/1.19 in Stage 1).

## Open Issues / Next Steps

### P0: Transition Rate Fix (Required before production)

Options:
1. **EMA-smoothed regime score** (~50 LOC): Compute weighted score from features, apply EMA with 30-60s window, threshold on smoothed score. This would naturally suppress rapid transitions while preserving signal.
2. **Longer holdoff** (5 LOC): Increase holdoff from 5s to 60s. Simple but delays reaction to genuine regime changes.
3. **Sticky regime with explicit exit** (~30 LOC): Once in a regime, require sustained signal (e.g., 10 consecutive ticks) to transition out.

Recommendation: Option 1 (EMA score) for best signal-to-noise tradeoff.

### P1: Toxicity Validation

`toxicity_ema50_x1000` cannot be validated without trade-tick data. Two paths:
- Export trade-type rows from ClickHouse when online
- Add trade-tick export format to `ch_batch_export.py`

### P2: Production Wiring

Not yet done:
- Wire `RegimeClassifier` into strategy runner (alongside `FeatureEngine`)
- Pass regime to `ExecutionOptimizer.decide()` at call sites
- Add Prometheus metrics for regime distribution and transition rate

## Files Delivered

| File | Type | LOC |
|------|------|-----|
| `src/hft_platform/execution/regime_classifier.py` | Production | ~200 |
| `src/hft_platform/execution/execution_optimizer.py` | Modified | +30 |
| `tests/unit/test_regime_classifier.py` | Test | ~190 |
| `tests/unit/test_execution_optimizer.py` | Modified | +55 |
| `research/experiments/validations/r24_diagnostics/diagnostic_0a_fill_quality.py` | Research | ~220 |
| `research/experiments/validations/r24_diagnostics/diagnostic_0b_exec_baseline.py` | Research | ~230 |
| `research/experiments/validations/r24_diagnostics/diagnostic_1_regime_backtest.py` | Research | ~210 |
| `docs/alpha-research/r24/diagnostic_0a_fill_quality.md` | Results | - |
| `docs/alpha-research/r24/diagnostic_0b_exec_baseline.md` | Results | - |
| `docs/alpha-research/r24/diagnostic_1_regime_backtest.md` | Results | - |
| `docs/alpha-research/r24/stage1_literature_survey.md` | Survey | - |
| `docs/alpha-research/r24/stage2_direction_c_prototype.md` | This report | - |
