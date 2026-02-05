# CLI Reference

CLI 入口：
- `hft`（由 `uv sync --dev` 安裝的 console script）
- 或 `python -m hft_platform`（等價）

建議使用：
```bash
uv run hft --help
```

---

## 1) `hft run` — 啟動主流程
```bash
hft run sim
hft run live
hft run replay
```

常用參數：
- `--strategy <id>`
- `--strategy-module <module>`
- `--strategy-class <class>`
- `--symbols <list>`

範例：
```bash
hft run sim --strategy demo --symbols 2330 2317
```

行為：
- 若缺 `SHIOAJI_API_KEY/SECRET_KEY`，`live` 會自動降級 `sim`。
- Prometheus metrics 預設啟動於 `:9090`。

---

## 2) `hft init` — 生成策略骨架
```bash
hft init --strategy-id my_strategy --symbol 2330
```

產生：
- `config/settings.py`
- `src/hft_platform/strategies/<strategy>.py`
- `tests/test_<strategy>.py`

---

## 3) `hft check` — 驗證設定
```bash
hft check
hft check --export json
hft check --export yaml
```

---

## 4) `hft wizard` — 互動式設定
```bash
hft wizard
```

---

## 5) `hft feed status` — Feed / Metrics 檢查
```bash
hft feed status --port 9090
```

---

## 6) `hft diag` — 快速診斷
```bash
hft diag
```

---

## 7) `hft strat test` — 策略 smoke test
```bash
hft strat test --symbol 2330
hft strat test --strategy-id demo --module hft_platform.strategies.simple_mm --cls SimpleMarketMaker
```

---

## 8) `hft backtest convert` — JSONL → NPZ
```bash
hft backtest convert \
  --input data/events.jsonl \
  --output data/events.npz \
  --scale 10000
```

---

## 9) `hft backtest run` — 內建 HftBacktest
```bash
hft backtest run \
  --data data/events.npz \
  --symbol 2330 \
  --report
```

策略模式：
```bash
hft backtest run \
  --data data/events.npz \
  --strategy-module hft_platform.strategies.simple_mm \
  --strategy-class SimpleMarketMaker \
  --strategy-id demo \
  --symbol 2330
```

---

## 10) `hft config resolve` — 解析交易所代碼
```bash
hft config resolve 2330 2317 --output config/symbols.yaml
```

> 需要 `SHIOAJI_API_KEY/SECRET_KEY`，會用 Simulation mode 登入。

---

## 11) `hft config build` — 生成 symbols.yaml
```bash
hft config build \
  --list config/symbols.list \
  --output config/symbols.yaml
```

選項：
- `--contracts` 指定合約快取
- `--metrics` 指定 metrics cache
- `--no-contracts` 跳過合約快取
- `--preview` 預覽

---

## 12) `hft config preview` — 預覽展開結果
```bash
hft config preview --sample 10
```

---

## 13) `hft config validate` — 驗證配置
```bash
hft config validate
hft config validate --online
```

---

## 14) `hft config sync` — 下載合約 + 重建
```bash
hft config sync --list config/symbols.list --output config/symbols.yaml
```

---

## 常見使用流程

### A. 模擬啟動
```bash
uv sync --dev
cp .env.example .env
hft config build --list config/symbols.list --output config/symbols.yaml
hft run sim
```

### B. Live 啟動
```bash
export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
export HFT_MODE=live
hft run live
```

### C. 生成策略與測試
```bash
hft init --strategy-id my_strategy --symbol 2330
hft strat test --symbol 2330
```
