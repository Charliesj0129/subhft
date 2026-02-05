# HFT Platform 使用者指南（完整流程）

本文件是「從零到可跑、可觀測、可回測」的完整流程。內容偏實作導向，包含：
- 本機模擬（不需券商憑證）
- Docker Compose 全堆疊（ClickHouse + Grafana）
- Live 模式啟用條件與安全邏輯
- Symbols/策略/回測/延遲量測

> Safety: 本專案預設是 `sim`。要進入 `live` 必須顯式設定 `HFT_MODE=live` 且有憑證。

---

## 0. 前置需求

### 必要
- Python 3.12+
- `uv`（依賴管理）

### 選用
- Docker + Docker Compose（完整觀測與 ClickHouse）
- Rust toolchain（若需在本機 rebuild rust extension）

檢查版本：
```bash
python --version
uv --version
```

---

## 1. 取得專案並安裝依賴
```bash
git clone <repo-url>
cd hft_platform

uv sync --dev
```

> `uv sync --dev` 會安裝 dev deps 與 CLI 入口 `hft`。
> 若 `hft` 沒在 PATH，可使用 `uv run hft ...` 或 `python -m hft_platform ...`。

---

## 2. 建立環境變數（.env）
```bash
cp .env.example .env
```

常用變數：
- `HFT_MODE=sim|live|replay`
- `HFT_ENV=dev|staging|prod`（overlay，不改交易模式）
- `SYMBOLS_CONFIG=config/symbols.yaml`
- `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY`（live 必要）
- `SHIOAJI_PERSON_ID` + `SHIOAJI_CA_PATH` + `SHIOAJI_CA_PASSWORD`（啟用 CA）

> `.env` 僅供本機使用，勿提交到版本控制。

---

## 3. Symbols 流程（最重要）

`config/symbols.list` 是唯一來源，`config/symbols.yaml` 由它生成。

### 3.1 編輯 `symbols.list`
```text
2330 exchange=TSE tags=stocks|tw50
TXF@front exchange=FUT tags=futures|front_month
OPT@TXO@near@ATM+/-5 exchange=OPT tags=options|near|atm
@include config/symbols.examples/tw50.list
```

### 3.2 生成 `symbols.yaml`
```bash
uv run hft config build --list config/symbols.list --output config/symbols.yaml
```

### 3.3 若需要 broker 合約快取（規則式展開）
```bash
uv run hft config sync --list config/symbols.list --output config/symbols.yaml
```

- 會寫入 `config/contracts.json`
- 可搭配 `--metrics` 使用進階選股規則

### 3.4 驗證
```bash
uv run hft config preview
uv run hft config validate
```

---

## 4. 本機模擬（最簡單）

```bash
uv run hft run sim
```

輸出會顯示當前 mode、symbols、strategy、prometheus port。

### 驗證
- Metrics: http://localhost:9090/metrics
- Log: console (structlog JSON)

---

## 5. Docker Compose 全堆疊（ClickHouse + Grafana）

```bash
docker compose up -d --build

docker compose logs -f hft-engine
```

服務與 port：
- `hft-engine` (metrics): 9090
- `clickhouse`: 8123/9000
- `prometheus`: 9091
- `grafana`: 3000
- `alertmanager`: 9093
- `redis`: 6379

**重要**：docker-compose 內預設 `SYMBOLS_CONFIG=config/base/symbols.yaml`。
若你要使用自己生成的 `config/symbols.yaml`，請在 `.env` 設：
```bash
SYMBOLS_CONFIG=config/symbols.yaml
```
並重啟容器：
```bash
docker compose restart hft-engine
```

---

## 6. 策略開發流程

### 6.1 產生策略樣板
```bash
uv run hft init --strategy-id my_strategy --symbol 2330
```

產生：
- `config/settings.py`
- `src/hft_platform/strategies/my_strategy.py`
- `tests/test_my_strategy.py`

### 6.2 Strategy 最小骨架
```python
from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy

class Strategy(BaseStrategy):
    def on_stats(self, event: LOBStatsEvent) -> None:
        if event.spread > 5:
            self.buy(event.symbol, event.best_bid, 1)
```

### 6.3 Smoke Test
```bash
uv run hft strat test --symbol 2330
```

---

## 7. Live 模式（需憑證）

### 7.1 憑證
```bash
export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
export HFT_MODE=live
```

### 7.2 CA（選用）
```bash
export SHIOAJI_PERSON_ID=...
export SHIOAJI_CA_PATH=/path/to/Sinopac.pfx
export SHIOAJI_CA_PASSWORD=...
export SHIOAJI_ACTIVATE_CA=1
```

### 7.3 啟動
```bash
uv run hft run live
```

> 若缺少 `SHIOAJI_*`，系統會自動降級 `sim` 並提示。

---

## 8. 回測（HftBacktest）

### 8.1 Convert JSONL → NPZ
```bash
uv run hft backtest convert \
  --input data/sample_events.jsonl \
  --output data/sample_feed.npz \
  --scale 10000
```

### 8.2 Run Backtest
```bash
uv run hft backtest run \
  --data data/sample_feed.npz \
  --strategy-module hft_platform.strategies.simple_mm \
  --strategy-class SimpleMarketMaker \
  --strategy-id demo \
  --symbol 2330 \
  --report
```

---

## 9. 資料流驗證（ClickHouse/WAL）

### 9.1 ClickHouse 事件量
```bash
docker exec clickhouse clickhouse-client --query \
  "SELECT count() FROM hft.market_data"
```

### 9.2 最新時間
```bash
docker exec clickhouse clickhouse-client --query \
  "SELECT max(fromUnixTimestamp64Nano(ingest_ts,'Asia/Taipei')) FROM hft.market_data"
```

### 9.3 WAL
- `.wal/` 會持續寫入 jsonl
- `wal-loader` 會回灌 ClickHouse

---

## 10. 延遲與抖動量測（真實化）

### 10.1 Shioaji API Probe
```bash
uv run python scripts/latency/shioaji_api_probe.py \
  --mode sim --iters 30 --warmup 3 --sleep 0.2
```
輸出：
- `reports/shioaji_api_latency.json`
- `reports/shioaji_api_latency.csv`

### 10.2 End-to-End 延遲分佈（ClickHouse）
```bash
uv run python scripts/latency/e2e_clickhouse_report.py \
  --window-min 10 --time-bucket-s 10 --latency-bucket-us 500
```
輸出：
- `reports/e2e_latency.summary.json`
- `reports/e2e_latency.market_data.heatmap.csv`

### 10.3 Symbol-level 延遲熱圖
```bash
uv run python scripts/latency_e2e_report.py --window-min 10
```
輸出：
- `reports/latency_e2e.json`
- `reports/latency_by_symbol.csv`
- `reports/latency_heatmap.txt`

---

## 11. Observability / Metrics

Metrics endpoint：`http://localhost:9090/metrics`

常用指標（部分）：
- `feed_events_total`
- `feed_latency_ns`
- `event_loop_lag_ms`
- `queue_depth{queue=...}`
- `execution_router_lag_ns`
- `order_actions_total`
- `recorder_rows_flushed_total`

完整清單：`docs/observability_minimal.md`

---

## 12. 測試與品質
```bash
uv run ruff check --fix
uv run pytest
```

---

## 13. 常見問題
- **看不到行情**：確認 `SYMBOLS_CONFIG` 指向正確的 `symbols.yaml`
- **今天 2/3 卻看到 2/4 時間**：通常是主機/容器時間或時區偏移（見 `docs/troubleshooting.md`）
- **live 自動降級 sim**：缺 `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY`

---

## 14. 下一步
- `docs/cli_reference.md` - CLI 參考
- `docs/config_reference.md` - 設定與環境變數
- `docs/strategy-guide.md` - 策略開發
- `docs/runbooks.md` - 運維與事故處理
