# backtest — HftBacktest Integration

> **Package**: `src/hft_platform/backtest/`
> **Runtime Plane**: Research
> **Files**: 10

## Overview

HftBacktest integration: JSONL to NPZ conversion, feed/elapse loops, equity tracking, and scorecard reporting.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `runner.py` | `BacktestRunner` | Main backtest orchestrator |
| `adapter.py` | `BacktestAdapter` | HftBacktest library adapter |
| `equity.py` | `EquityTracker` | PnL and equity curve tracking |
| `convert.py` | — | JSONL → NPZ data conversion |
| `scorecard.py` | — | Performance metrics (Sharpe, drawdown, etc.) |
| + 5 more | — | Feed loops, elapse loops, utilities |

## Usage

```bash
uv run hft backtest run --alpha <alpha_id> --data <path>
```

## Data Pipeline

```
ClickHouse (historical) → JSONL export → NPZ convert → HftBacktest engine
  → EquityTracker → Scorecard → Gate C evaluation
```

## Latency Realism

Per Latency Realism Guard:
- Model place/update/cancel latencies separately
- Use P95 latency assumptions for promotion
- Record assumptions in research artifacts
- Missing latency profile = non-promotion-ready
