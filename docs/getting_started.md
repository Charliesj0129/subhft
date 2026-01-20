# HFT Platform Getting Started

本文件提供完整上手流程（從零到可跑），並補充常見操作與驗證方法。

## 1. 前置需求
- Python 3.10+
- uv（建議）
- Docker（選用：需要 ClickHouse/完整堆疊時）

檢查版本：
```bash
python --version
uv --version
```

## 2. 下載與安裝
```bash
git clone <repo-url>
cd hft_platform

# 開發環境依賴
uv sync --dev
```

## 3. 建立環境變數
```bash
cp .env.example .env
```
`.env` 只用於本機開發，勿提交到版本控制。

常用變數：
- `HFT_MODE=sim`（預設）
- `SYMBOLS_CONFIG=config/symbols.yaml`
- `SHIOAJI_*`（實盤才需要）

## 4. 設定 symbols 與策略
### 4.1 symbols 設定
`config/symbols.list` 是唯一來源，`config/symbols.yaml` 由它生成：
```
2330 exchange=TSE tags=stocks
TXF@front exchange=FUT tags=futures|front_month
```

產生 YAML：
```bash
make symbols
```

**快速批量方式**
- `python -m hft_platform config preview`
- `python -m hft_platform config validate`
- `make sync-symbols`（更新券商合約 cache + rebuild）
- `python -m hft_platform wizard`（preset/手動/檔案匯入）

### 4.2 策略設定
設定路徑來源依序為：
1. `config/base/main.yaml`
2. `config/env/<mode>/main.yaml`（若存在）
3. `config/settings.py`（可選）
4. 環境變數 `HFT_*`
5. CLI 參數

`config/base/main.yaml` 範例：
```yaml
mode: sim
symbols: ["2330"]
strategy:
  id: simple_mm_demo
  module: hft_platform.strategies.simple_mm
  class: SimpleMarketMaker
  params:
    subscribe_symbols: ["2330"]
paths:
  symbols: config/base/symbols.yaml
  strategy_limits: config/base/strategy_limits.yaml
  order_adapter: config/base/order_adapter.yaml
prometheus_port: 9090
```

你也可以用 CLI 產生 `config/settings.py`：
```bash
python -m hft_platform init --strategy-id my_strategy --symbol 2330
```

## 5. 一鍵啟動（Docker）
```bash
make start
```
此命令會建置 image、啟動 ClickHouse，並啟動主程式。

## 6. 啟動模擬模式（本機）
```bash
make run-sim
```

輸出會顯示目前 mode、symbols、strategy 與 prometheus port。

### 驗證
- Prometheus：`http://localhost:9090/metrics`
- Log：終端機或 `logs/`（若有啟用檔案輸出）

## 7. 設定/驗證策略是否能發單
使用內建策略測試：
```bash
python -m hft_platform strat test --symbol 2330
```

## 8. 啟用 ClickHouse（選用）
使用 Docker Compose：
```bash
docker compose up -d
```

ClickHouse ports：
- 8123 (HTTP)
- 9000 (Native)

如果只要 WAL，不連 ClickHouse：
```bash
export HFT_CLICKHOUSE_ENABLED=0
```

## 9. 實盤模式
設定 .env 或 shell 環境：
```bash
export SHIOAJI_PERSON_ID="YOUR_ID"
export SHIOAJI_PASSWORD="YOUR_PWD"
```

啟動：
```bash
python -m hft_platform run live
```

若找不到憑證，會自動降級為 sim。

## 10. 回測
```bash
python -m hft_platform backtest run --data data/sample_feed.npz --symbol 2330 --report
```

若你有 JSONL 事件檔，可先轉換：
```bash
python -m hft_platform backtest convert --input events.jsonl --output data.npz --scale 10000
```

## 11. 測試與檢查
```bash
make test
make coverage
```

## 12. 建議工作流程
1) 修改 `config/symbols.list`，並執行 `make symbols`。
2) 以 sim 模式驗證策略行為。
3) 觀察 metrics 與風控拒單。
4) 需要時再切換 live。

## 13. 常見陷阱
- **無事件輸入**：symbols 設定不在交易所合約中。
- **價格縮放不一致**：確認 `price_scale` / `tick_size`。
- **無法連 ClickHouse**：檢查 `HFT_CLICKHOUSE_*`。

更多細節：
- `docs/feature_guide.md`
- `docs/config_reference.md`
- `docs/cli_reference.md`
