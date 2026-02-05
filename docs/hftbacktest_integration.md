# HftBacktest Integration (Usage)

本專案提供 `hft backtest` CLI，支援：
- 將 JSONL 事件轉換為 hftbacktest NPZ
- 使用策略 adapter 進行回測

---

## 1) Convert JSONL → NPZ

輸入為平台標準 JSONL（通常來自 WAL 或 replay），輸出 NPZ：

```bash
uv run hft backtest convert \
  --input data/sample_events.jsonl \
  --output data/sample_feed.npz \
  --scale 10000
```

---

## 2) Run Backtest

### 2.1 直接回測
```bash
uv run hft backtest run \
  --data data/sample_feed.npz \
  --symbol 2330 \
  --report
```

### 2.2 策略 Adapter 模式
```bash
uv run hft backtest run \
  --data data/sample_feed.npz \
  --strategy-module hft_platform.strategies.simple_mm \
  --strategy-class SimpleMarketMaker \
  --strategy-id demo \
  --symbol 2330
```

> Strategy adapter 目前一次只支援單一資產。

---

## 3) 常見參數
- `--tick-size` / `--lot-size`
- `--latency-entry` / `--latency-resp`
- `--fee-maker` / `--fee-taker`
- `--report` 生成 HTML

---

## 4) 輸出
- 回測結果會輸出至 console
- `--report` 會生成 HTML (若支援)

---

更多細節：`hft backtest --help`
