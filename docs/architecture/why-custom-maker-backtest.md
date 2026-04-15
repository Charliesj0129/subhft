# Why Custom Maker Backtest Engine (Not hftbacktest)

**Date**: 2026-04-15
**Context**: R47 calibration crisis — same strategy produced -27K to +61K PnL depending on method.

## The Problem

The `hftbacktest` library (v2.4) is used for our taker strategies via `HftNativeRunner`. It works well for threshold-crossing alpha strategies that enter/exit on market orders. But for **maker strategies** (passive limit orders), its fill model is fundamentally broken for TAIFEX micro-futures.

## Why hftbacktest PowerProbQueueModel Fails for TAIFEX Maker

### 1. 14x Pessimistic Bias (Measured)

R47 calibration (2026-04-10) showed:
- hftbacktest PowerProbQueueModel(3.0): **-27,366 pts** (D2 gate kills 94.7% of quotes)
- CK-direct half-queue (qf=0.5): **+4,504 pts**
- Ratio: **~14x too pessimistic**

Root cause: PowerProbQueueModel was designed for US equity markets with deep, liquid order books. TAIFEX micro-futures (TMFD6/TXFD6) have:
- Spread typically 1-5 ticks (not 10-50 as in US equities)
- Queue depth 5-50 contracts (not 500-5000)
- Tick size = 1 point (discrete, not sub-penny)

The model's queue survival probability decays too aggressively for shallow queues.

### 2. D2 Gate Over-Suppression

The PowerProbQueueModel's depletion probability (D2 gate in R47) triggers suppression at P(depl) > 0.7. On TAIFEX with shallow queues, this fires on **94.7%** of quoting opportunities — effectively disabling the strategy.

In production, our R47 strategy runs with D2 disabled (`queue_cancel_threshold=1.0`) because the signal is too noisy on TAIFEX book depth. The hftbacktest model doesn't know this.

### 3. No TAIFEX-Specific Calibration

hftbacktest's fill models assume:
- Continuous price process (not discrete tick grid)
- Deep liquidity (queue position doesn't matter much)
- US-style maker rebates (negative fees)

TAIFEX reality:
- Discrete 1-tick spread (price moves in jumps)
- Shallow queues (queue position is everything)
- No maker rebates (retail pays both sides)
- 4.0 pts RT cost on TMFD6 (40% of typical spread)

### 4. Export Data Format Mismatch

The hftbacktest `.npz` format requires specific event types (DEPTH_EVENT, TRADE_EVENT). R47's 2026-04-09 data regeneration exposed a critical bug: `DEPTH_EVENT` for L2-L5 caused level accumulation (17 levels instead of 5), collapsing spread from 4 to 1 pt. This invalidated ALL prior hftbacktest results.

Our CK-direct approach reads directly from ClickHouse using the native tick/bidask format, avoiding this translation layer entirely.

## What We Built Instead

### MakerEngine (CK-Direct Queue Depletion)

```
ClickHouse (tick + bidask) → MakerEngine → QueueDepletionFill(qf) → FIFO PnL → BacktestResult
```

Key design decisions:
1. **Queue fraction parameter (qf)**: Configurable assumption about queue entry position. Default 0.5 = mid-queue. No probability model — just a deterministic assumption that's transparent and comparable.
2. **Direct ClickHouse data**: No format conversion. Reads the same data that the live system records. No translation bugs.
3. **Strategy-agnostic**: Engine handles fill simulation. Strategy logic injected via `MakerStrategy` protocol. Not hardcoded to R47.
4. **Full provenance**: Every result includes method, fill model, instrument, data period in `backtest_report.json`.

### When to Use Each Engine

| Scenario | Engine | Why |
|----------|--------|-----|
| Taker strategy (IC→threshold→market order) | TakerEngine (hftbacktest) | Fill model works for aggressive execution |
| Maker strategy (passive limit orders) | MakerEngine (CK-direct) | Queue depletion model calibrated to TAIFEX |
| Historical comparison | Check `backtest_report.json` `fill_model` field | Don't compare results across methods |

## The hftbacktest Dependency Stays

We keep `hftbacktest>=2.4,<3` because:
- `HftNativeRunner` + `HftBacktestAdapter` are the standardized taker engine
- Queue model analysis tools use it for comparison/research
- Synthetic data generation scripts depend on it

We just don't use it for maker strategy validation anymore.

## Lessons from R47 Calibration Crisis

1. **One strategy, six PnL numbers**: -27,366 / -1,908 / +4,504 / +9,912 / +14,755 / +29,747 — all from different methods on the same strategy
2. **Fill model is the biggest single source of backtest bias** — bigger than cost model, latency model, or data quality
3. **Transparent assumptions > sophisticated models**: qf=0.5 is crude but everyone knows what it means. PowerProbQueueModel(3.0) is a black box that was 14x wrong.
4. **Standardization prevents drift**: `make research` is the sole entry point. Results include method provenance. Future conversations can trace any PnL claim to a specific `run_id`.
