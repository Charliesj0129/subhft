# HftBacktest Integration

本專案提供 `hft backtest` 命令支援資料轉換與策略回測。

## 1) JSONL → NPZ
```bash
uv run hft backtest convert \
  --input data/sample_events.jsonl \
  --output data/sample_feed.npz \
  --scale 10000
```

## 2) 執行回測

### 2.1 基本模式
```bash
uv run hft backtest run \
  --data data/sample_feed.npz \
  --symbol 2330 \
  --report
```

### 2.2 策略 adapter 模式
```bash
uv run hft backtest run \
  --data data/sample_feed.npz \
  --strategy-module hft_platform.strategies.simple_mm \
  --strategy-class SimpleMarketMaker \
  --strategy-id demo \
  --symbol 2330
```

## 3) 常用參數
- `--tick-size` / `--lot-size`
- `--tick-sizes` / `--lot-sizes` / `--symbols`（多資產）
- `--latency-entry` / `--latency-resp`
- `--fee-maker` / `--fee-taker`
- `--record-out`（保存 recorder output npz）
- `--no-partial-fill`
- `--strict-equity`
- `--report`

## 4) 注意事項
- 提供 `--strategy-module` 時，現行 adapter 仍以單資產流程最穩定。
- 回測輸入資料應為已正規化事件格式。
