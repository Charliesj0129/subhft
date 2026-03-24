# CLI Reference

入口：
- `hft`
- `python -m hft_platform`（等價）

建議：
```bash
uv run hft --help
```

## 1) `hft run` — 啟動主流程
```bash
hft run sim
hft run live
hft run replay
```
常用參數：
- `--mode sim|live|replay`
- `--strategy <id>`
- `--strategy-module <module>`
- `--strategy-class <class>`
- `--symbols <list>`

行為：
- 缺 `SHIOAJI_API_KEY/SHIOAJI_SECRET_KEY` 時，`live` 會自動降級 `sim`。

## 2) `hft init` — 產生策略骨架
```bash
hft init --strategy-id my_strategy --symbol 2330
```
產生：
- `config/settings.py`
- `src/hft_platform/strategies/<strategy>.py`
- `tests/test_<strategy>.py`

## 3) `hft check` — 驗證設定
```bash
hft check
hft check --export json
hft check --export yaml
```

## 4) `hft wizard` — 互動式設定
```bash
hft wizard
```

## 5) `hft feed status` — Feed/Metrics 快檢
```bash
hft feed status --port 9090
```

## 6) `hft diag` — 診斷與 timeline
```bash
hft diag
hft diag --trace-file outputs/decision_traces/xxx.jsonl --limit 50
hft diag --trace-file outputs/decision_traces/xxx.jsonl --timeline --timeline-format md --out timeline.md
```

## 7) `hft feature` — Feature Plane 治理
```bash
hft feature profiles --json
hft feature validate
hft feature preflight --strategies config/base/strategies.yaml
hft feature rollout-status
hft feature rollout-set --feature-set alpha_lob_v1 --state active --profile-id default
hft feature rollout-rollback --feature-set alpha_lob_v1
```

## 8) `hft strat test` — 策略 smoke test
```bash
hft strat test --symbol 2330
hft strat test --strategy-id demo --module hft_platform.strategies.simple_mm --cls SimpleMarketMaker
```

## 9) `hft backtest convert` — JSONL → NPZ
```bash
hft backtest convert --input data/events.jsonl --output data/events.npz --scale 10000
```

## 10) `hft backtest run` — HftBacktest
```bash
hft backtest run --data data/events.npz --symbol 2330 --report
```
常用參數：
- `--strategy-module` / `--strategy-class` / `--strategy-id`
- `--tick-size` / `--lot-size`
- `--tick-sizes` / `--lot-sizes` / `--symbols`（多資產）
- `--latency-entry` / `--latency-resp`
- `--fee-maker` / `--fee-taker`
- `--record-out`
- `--no-partial-fill`
- `--strict-equity`

## 11) `hft recorder status` — WAL/ClickHouse 狀態
```bash
hft recorder status
hft recorder status --wal-dir .wal --ck-host localhost
```

## 12) `hft config` — 設定與 symbols 工具

### `resolve`
```bash
hft config resolve 2330 2317 --output config/symbols.yaml
```

### `build`
```bash
hft config build --list config/symbols.list --output config/symbols.yaml
```

### `preview`
```bash
hft config preview --sample 10
```

### `validate`
```bash
hft config validate
hft config validate --online
```

### `sync`
```bash
hft config sync --list config/symbols.list --output config/symbols.yaml
```

### `contracts-status`
```bash
hft config contracts-status --contracts config/contracts.json --stale-after-s 86400
```

## 13) `hft alpha` — 研究工廠治理
```bash
hft alpha list
hft alpha scaffold my_alpha --paper arxiv:2408.03594
hft alpha search --mode random --data data/train.npy --feature-fields f1,f2 --trials 100
hft alpha validate --alpha-id my_alpha --data data/train.npy data/test.npy
hft alpha promote --alpha-id my_alpha --owner charlie
hft alpha rl-promote --alpha-id my_alpha --owner charlie
hft alpha pool matrix
hft alpha canary status
hft alpha ab-compare RUN_A RUN_B
hft alpha experiments list
```

## 常見流程

### A. 本機模擬
```bash
uv sync --dev
cp .env.example .env
hft config build --list config/symbols.list --output config/symbols.yaml
hft run sim
```

### B. Live
```bash
export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
export HFT_MODE=live
hft run live
```

### C. 策略 + 回測
```bash
hft init --strategy-id my_strategy --symbol 2330
hft strat test --symbol 2330
hft backtest run --data data/events.npz --symbol 2330 --report
```
