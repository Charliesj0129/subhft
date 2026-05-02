# TAIFEX Queue Model Calibration Plan

**Goal**: Calibrate hftbacktest's PowerProbQueueModel for TAIFEX, then cross-validate against our CK-direct MakerEngine.

## Current Status

| Method | Parameter | R47 Result | Issue |
|--------|-----------|-----------|-------|
| PowerProbQueueModel(3.0) | n=3.0 | -27,366 pts | Too pessimistic (D2 kills 94.7% quotes) |
| CK-direct QueueDepletion(0.5) | qf=0.5 | +4,504 pts | Deterministic, no probability model |

**Hypothesis**: n=3.0 is too aggressive for TAIFEX's shallow queues. Lower n (1.0-2.0) or log model may be correct.

## Prerequisites

1. Clean R47 live fill data (orphan fill bug fixed 2026-04-15)
2. 30+ trading days of paper/live fills
3. ClickHouse with matching tick+bidask data for the same days

## Step 1: Collect Live Calibration Data

For each R47 fill in live/shadow mode, record:
- `fill_ts`: fill timestamp
- `order_ts`: order placement timestamp
- `side`: buy/sell
- `price`: fill price
- `book_qty_at_placement`: BBO quantity when order was placed
- `queue_position_est`: estimated queue position (from ClickHouse trade volume between placement and fill)

```sql
-- Example: estimate queue position from trade volume
SELECT
    SUM(volume) AS consumed_volume
FROM hft.market_data
WHERE symbol = 'TMFD6'
  AND type = 'Tick'
  AND exch_ts BETWEEN {order_ts} AND {fill_ts}
  AND price_scaled = {fill_price}
```

## Step 2: Measure Actual Fill Rate by Queue Fraction

Group fills by estimated queue entry point:

| Queue Entry Fraction | Expected Fills | Actual Fills | Fill Rate |
|---------------------|----------------|--------------|-----------|
| 0.0-0.2 (front) | high | ? | ? |
| 0.2-0.4 | medium-high | ? | ? |
| 0.4-0.6 (mid) | medium | ? | ? |
| 0.6-0.8 | medium-low | ? | ? |
| 0.8-1.0 (back) | low | ? | ? |

## Step 3: Sweep PowerProbQueueModel Parameter

Run R47 strategy through hftbacktest with different n values:

```python
for n in [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]:
    asset = BacktestAsset().power_prob_queue_model(n)
    result = run_backtest(asset, r47_strategy, dates)
    compare_with_live(result, live_fills)
```

Metrics to compare:
- `|backtest_fill_rate - live_fill_rate|` → minimize
- `|backtest_pnl_per_fill - live_pnl_per_fill|` → minimize
- `|backtest_adverse_fill_pct - live_adverse_fill_pct|` → minimize

## Step 4: Also Test Log Models

```python
# LogProbQueueModel has no parameter — just test it
asset_log = BacktestAsset().log_prob_queue_model()
asset_log2 = BacktestAsset().log_prob_queue_model2()
```

## Step 5: Cross-Validate with CK-Direct

Compare calibrated hftbacktest results with CK-direct:

| Method | PnL | Fill Rate | Adverse Fill % | vs Live |
|--------|-----|-----------|----------------|---------|
| PowerProb(n_best) | ? | ? | ? | ? |
| LogProb2 | ? | ? | ? | ? |
| CK-direct(qf=0.5) | +4,504 | ? | ? | ? |
| **Live actual** | ? | ? | ? | baseline |

## Step 6: Update Standardized Pipeline

If calibrated hftbacktest is more accurate:
1. Update `BacktestConfig` with calibrated n parameter
2. Optionally make MakerEngine support hftbacktest backend
3. Store calibration result in `config/research/queue_model_calibration.yaml`

If CK-direct is still better:
1. Document why (with data)
2. Keep current architecture

## Data Requirements

| Data | Source | Days Needed |
|------|--------|-------------|
| R47 live fills | production logs | 30+ |
| Matching tick+bidask | ClickHouse | same days |
| TMFD6 queue depth at fill time | ClickHouse L1 | same days |

## Timeline

- Blocked on: accumulating 30+ days of clean R47 live fills (post orphan-fix)
- Estimated data collection: 30 trading days ≈ 6 weeks
- Calibration analysis: 1-2 days once data available
- Target: June 2026

## Export Data for hftbacktest

The export bug (DEPTH_EVENT accumulation) was fixed. Use:

```bash
python research/tools/ch_batch_export.py --symbols TMFD6 --host localhost --formats l2
```

Verify export quality:
```python
import numpy as np
data = np.load('path/to/export.npz')['data']
# Check spread distribution
spreads = data[data['ev'] & 1 > 0]  # DEPTH_EVENT
# Should show spread=1-5 ticks, not collapsed to 1
```
