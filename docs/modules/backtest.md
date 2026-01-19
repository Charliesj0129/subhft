# backtest

## Purpose
Offline backtesting pipeline built on `hftbacktest`, with utilities for data conversion and reporting.

## Key Files
- `src/hft_platform/backtest/adapter.py`: Strategy bridge (`StrategyHbtAdapter`) to run a strategy in backtest mode.
- `src/hft_platform/backtest/convert.py`: Convert JSONL event streams to NPZ for `hftbacktest`.
- `src/hft_platform/backtest/runner.py`: `HftBacktestRunner` and config parsing.
- `src/hft_platform/backtest/reporting.py`: HTML/summary reporting.

## Typical Flow
1) Convert data to NPZ.
2) Run backtest with runner or strategy adapter.
3) Inspect report output.

## CLI Examples
```bash
python -m hft_platform backtest convert --input events.jsonl --output data.npz --scale 10000
python -m hft_platform backtest run --data data/sample_feed.npz --symbol 2330 --report
```

## Inputs and Outputs
- Input: NPZ files or JSONL events (converted).
- Output: console stats and optional report files.

## Configuration
- `--tick-size`, `--lot-size`, `--price-scale` influence PnL calculation.
- Use consistent `price_scale` with `symbols.yaml`.

## Extension Points
- Add custom report generation in `reporting.py`.
- Implement new adapters for multi-asset or portfolio backtests.
