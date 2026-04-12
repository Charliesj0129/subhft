# execution — Fill Processing & Position Tracking

> **Package**: `src/hft_platform/execution/`
> **Runtime Plane**: Execution
> **Hot-Path**: `PositionStore.on_fill()`, `ExecutionOptimizer.decide()`

## Overview

Order execution, fill normalization, integer-only position tracking, reconciliation, and execution optimization. 14 files covering the full execution lifecycle.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `normalizer.py` | `ExecutionNormalizer`, `RawExecEvent` | Shioaji callbacks → FillEvent/OrderEvent |
| `positions.py` | `Position`, `PositionStore` | Dual-track position accounting (Python + Rust) |
| `router.py` | `ExecutionRouter` | Inbound fill/order routing with dedup and DLQ |
| `reconciliation.py` | `ReconciliationService`, `PositionDiscrepancy` | Periodic broker vs local sync |
| `execution_optimizer.py` | `ExecutionOptimizer` | Limit vs market order decision (Albers 2025) |
| `regime_classifier.py` | `RegimeClassifier`, `Regime` | LOB microstate classification |
| `gateway.py` | `ExecutionGateway` | OrderAdapter lifecycle wrapper |
| `checkpoint.py` | `PositionCheckpointWriter` | Atomic position persistence (SHA-256 integrity) |
| `startup_recon.py` | `StartupPositionVerifier` | Crash recovery position verification |
| `eod_recon.py` | `EODReconciliationRunner` | End-of-day reconciliation trigger |
| `fill_dlq.py` | `OrphanedFillDLQ` | Dead-letter queue for unresolved fills |
| `mtm.py` | `MarkToMarketCalculator` | Unrealized PnL computation |
| `slippage_tracker.py` | `SlippageTracker` | Per-fill slippage metrics |
| `trigger_executor.py` | `TriggerExecutor`, `TriggerCondition` | Price-triggered order execution |

## Position Tracking

```python
store = PositionStore()
delta = store.on_fill(fill_event)  # Returns PositionDelta
store.mark_to_market(mid_prices)   # Portfolio unrealized PnL
store.snapshot_positions()         # Atomic read under lock
```

### PnL Calculation (Scaled Integer)

- **LONG close** (SELL): `PnL = (fill_price - avg_price) x close_qty x multiplier`
- **SHORT close** (BUY): `PnL = (avg_price - fill_price) x close_qty x multiplier`
- **Weighted avg price**: `new_avg = (2*total_val + net_qty) // (2*net_qty)` (rounding correction)
- **Futures multipliers**: TMF=10, MXF=50, TXF=200

### Dual-Track (Python + Rust)

- Atomic `_fill_lock` protects Rust/Python consistency
- Rust `RustPositionTracker` preferred if available
- Python fallback with identical logic

## ExecutionOptimizer

Empirically calibrated limit-vs-market decision engine:

```python
optimizer = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.5)
order_type = optimizer.decide(spread_pts, near_depth, opp_depth, imbalance_ppm, side, ts_ns)
```

Decision logic:
1. Spread <= threshold → MARKET
2. fill_score = (Q_opp / Q_near) x 1000 (integer arithmetic)
3. Favorable imbalance bonus (+500)
4. ADVERSE regime forces MARKET; FAVORABLE relaxes thresholds

## RegimeClassifier

Classifies LOB microstate using FeatureEngine v2+ features:

| Feature | Index | Role |
|---------|-------|------|
| `tob_survival_ms` | 18 | Strongest predictor (rho=-0.21) |
| `toxicity_ema50_x1000` | 21 | Informed flow intensity |
| `ret_autocov_5s_x1e6` | 17 | Return autocovariance |
| `spread_ema300s` | 26 | Long-term spread regime (v3) |

→ **ADVERSE**: burst OR toxicity > threshold OR tob_survival < 50ms
→ **FAVORABLE**: tob_survival > 500ms AND |ret_autocov| < 500
→ **NEUTRAL**: everything else

## Reconciliation

### Periodic Sync (`ReconciliationService`)
- Fetches broker positions → compares with local
- **Critical** (sign flip or >10%): triggers StormGuard HALT
- **Non-critical**: consecutive drift → reduce_only mode after 2 observations
- Exponential backoff on failures (grace=10 before HALT)

### Startup Recovery (`StartupPositionVerifier`)
- Dual-source merge: checkpoint + broker
- Graduated response: critical mismatch → HALT, minor → auto-correct
- Fallback chain: dual → broker_only → checkpoint_only → empty (HALT)
- Restores portfolio aggregates for StormGuard drawdown continuity

### EOD Reconciliation
- Once-per-day at configurable UTC hour (default 05:00 = TWSE 13:00)
- Prometheus gauges: `eod_recon_status`, `eod_recon_last_ts`

## Checkpoint

```python
writer = PositionCheckpointWriter(store, path=".runtime/position_checkpoint.json")
path = writer.write_checkpoint()  # Atomic temp+rename+fsync
```

- SHA-256 integrity hash
- Includes portfolio aggregates (peak_equity, total_rpnl)
- Fees included for complete recovery
