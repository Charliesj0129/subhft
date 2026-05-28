---
name: hft-backtest-validation
description: Use when interpreting backtest results, calibrating fill models against CK-direct ground truth, running sub-gate evaluation, or diagnosing the 14× pessimism / 577× optimism bias profile. Renamed from `hft-backtest-calibration` in Stage 7 of the 2026-05-28 consolidation.
---

# HFT Backtest Validation

Validation + calibration rules for interpreting backtest results on TAIFEX.
For engine **configuration** (queue / latency / exchange models, contract
spec, adapter wiring) use **`hft-backtest-engine`** instead.

Derived from R47 calibration regression (PowerProb 14× too pessimistic) and
the CBS mid-price trap (+3 bps mid → −48 bps bid/ask). The canonical bias
matrix lives in `docs/runbooks/backtest-engine-selection.md`.

## Backtest Engine Hierarchy

| Engine | Fidelity | Speed | When to use |
|--------|----------|-------|-------------|
| **ClickHouse direct** | Ground truth | Slow | Final validation of maker strategies |
| **hftbacktest (calibrated)** | High (after calibration) | Medium | Parameter sweeps, walk-forward |
| **hftbacktest (default)** | **14x pessimistic for makers** | Medium | Directional strategies only |
| **Signal-only IC** | Indicative only | Fast | Initial screening (not for PnL claims) |

### Rule: CK Direct = Ground Truth

For maker strategies, hftbacktest PowerProbQueueModel(3.0) is reference-model only:
- R47 CK direct: **+4,504 pts** (ground truth)
- R47 hftbacktest default: **-27,366 pts** (14x pessimistic)
- R47 hftbacktest calibrated (half-queue, spread≥3): **+4,534 pts** (≈ CK)

**Always validate maker PnL against CK before claiming profitability.**

## Fill Model Selection

### For Maker Strategies (R47-style)

```yaml
# Calibrated configuration
queue_model: "PowerProbQueueModel(3.0)"
exchange_model: "NoPartialFillExchange"

# Critical calibration parameters:
# 1. Half-queue assumption: order fills when queue reaches 50% (not 100%)
# 2. Spread filter: only count fills when spread ≥ 3 pts
# 3. These two adjustments eliminate the 14x pessimism
```

### For Taker Strategies (directional)

```yaml
# Default hftbacktest config is fine for takers
queue_model: "PowerProbQueueModel(3.0)"  # Conservative → good for takers
exchange_model: "NoPartialFillExchange"
# No calibration needed — taker fills are deterministic (cross spread)
```

### For Mid-Price Strategies (FORBIDDEN for edge < 2x spread)

```python
# CBS R14 lesson:
# Mid-price model: +3.00 bps (looks profitable)
# Bid/ask model:   -47.70 bps (actually losing money)
# The "edge" was a modeling artifact

if estimated_edge < 2 * median_spread:
    # MUST use bid/ask execution model
    entry = ask if buying else bid
    exit = bid if closing_long else ask
```

## Latency Modeling

### Mandatory Latency Profile

Every backtest MUST declare a latency profile:

```yaml
# config/research/latency_profiles.yaml
shioaji_sim_p95_v2026_03_04:
  submit_ack_latency_ms: 36      # P95 order → ACK
  modify_ack_latency_ms: 43      # P95 modify → ACK
  cancel_ack_latency_ms: 47      # P95 cancel → ACK
  local_decision_pipeline_us: 250 # Internal pipeline
  live_uplift_factor: 1.5         # Live/backtest scaling
```

### Latency Impact Rules

```
System internal latency: ~tens of μs (fast, negligible)
Shioaji sim API RTT: ~tens of ms (500x+ larger, dominates)

Rules:
1. Model place/modify/cancel latencies SEPARATELY
2. Use P95 for promotion decisions, P99 for stress tests
3. Sub-broker-RTT alpha half-lives are optimistic until shadow-validated
4. Missing latency profile = Gate D blocker (non-promotion-ready)
```

## Walk-Forward Analysis Configuration

```python
# Standard configuration
WalkForwardConfig:
    n_folds: 5                    # Expanding window folds
    is_oos_split: 0.7             # 70% IS, 30% OOS
    min_oos_days: 5               # Minimum OOS evaluation window

# CPCV (Combinatorial Purged Cross-Validation)
CPCVConfig:
    n_groups: 6                   # Contiguous date groups
    embargo_pct: 0.01             # 1% embargo between train/test
    purge_pct: 0.005              # 0.5% purge overlap
    # C(6,3) = 20 combinations
    # Output: PBO (Probability of Backtest Overfitting)
```

## Statistical Validation Checklist

### Mandatory Tests

| Test | Purpose | Pass criteria |
|------|---------|---------------|
| Detrended IC | Trend contamination check | Detrended IC > 0.01, sign preserved |
| BDS independence | Signal residual i.i.d. check | p > 0.05 (fail to reject H0) |
| Benjamini-Hochberg | Multiple comparison correction | Survived BH at α=0.05 |
| Walk-forward consistency | Overfitting detection | Positive Sharpe in >60% of folds |
| Regime split | Regime robustness | Profitable in both high/low vol |

### Common Traps

| Trap | Example | Detection |
|------|---------|-----------|
| **Subsampling inflation** | R32b: IC=0.092 (bar) vs 0.013 (tick) = 7x | Compare bar-IC vs tick-IC |
| **EMA trend contamination** | MLOFI: IC=0.206 raw vs -0.032 detrended | Detrended IC gate |
| **MFE ≠ PnL** | TMFD6: MFE=+18 pts but PnL=-27 pts | Full path-dependent backtest |
| **Regime non-stationarity** | TMFD6 spread: Jan 68 pts → Mar 3 pts | Recent-month validation |
| **Level accumulation** | Depth export: 17 levels instead of 5 | Verify book depth post-export |

## Backtest Data Pipeline

### Data Preparation

```bash
# 1. Export from ClickHouse
make research-stamp-data-meta DATA_PATH=path/to/data.npy

# 2. Validate data metadata sidecar
make research-validate-data-meta DATA_PATH=path/to/data.npy

# 3. Run governed backtest
make research ALPHA=<id> OWNER=<you> DATA='path/to/data.npy'
```

### Data Inventory (as of 2026-04-12)

```
TMFD6: 24 trading days (golden parquet, x1e6 scale)
TXFD6: 19 trading days (golden parquet, x1e6 scale)
TXO:   Limited (58 days OI data scraped, tick data sparse)
```

### Golden Parquet Convention

```python
# Golden parquet prices are x1,000,000 (NOT x10,000)
# Platform prices are x10,000
# Conversion: golden_price / 100 = platform_price
```

## Scorecard Interpretation

### Gate D Thresholds

| Metric | Threshold | Interpretation |
|--------|-----------|----------------|
| Sharpe OOS | ≥ 1.0 | Risk-adjusted return |
| Max Drawdown | ≥ -20% | Capital preservation |
| Turnover | ≤ 2.0 | Cost efficiency |
| Pool Correlation | ≤ 0.7 | Non-redundancy |
| Latency Profile | Must declare | Execution realism |

### Red Flags in Scorecard

```
IC monotonically increasing with horizon → trend contamination
Sharpe IS >> Sharpe OOS (> 3x) → overfitting
Walk-forward consistency < 40% → unstable
BDS p-value < 0.01 → residual dependence (model misspecification)
PBO > 50% → probably overfit
```

## Anti-Patterns

- Do NOT trust hftbacktest default output for maker strategies without CK calibration
- Do NOT use mid-price execution for edge < 2x spread
- Do NOT run backtests without declared latency profile
- Do NOT claim profitability from IC alone — run full backtest with realistic fills
- Do NOT compare IS and OOS Sharpe without checking regime stationarity
- Do NOT reuse pre-2026-04-10 hftbacktest results (depth export bug invalidated all)
