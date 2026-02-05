# Configuration Reference

本文件整理設定來源、環境變數與 config 檔案格式。原則是：
- **程式碼只有一份**，差異由 config / env / CLI 控制。
- **機密永遠在環境變數**。

---

## 1. 設定優先序 (Precedence)
由低到高（後者覆蓋前者）：
1) `config/base/main.yaml`
2) `config/env/<mode>/main.yaml`（sim/live）
2.5) `config/env/<env>/main.yaml`（dev/staging/prod via `HFT_ENV`）
3) `config/settings.py`（本機/個人覆蓋，可選）
4) Env vars (`HFT_*`, `SHIOAJI_*`, ...)
5) CLI 參數

對應實作：`src/hft_platform/config/loader.py`

---

## 2. 主要 Config 檔案
- `config/base/main.yaml`：主流程預設
- `config/env/<mode>/main.yaml`：sim/live 差異
- `config/settings.py`：本機自訂（可選）
- `config/symbols.list`：唯一 symbols 來源
- `config/symbols.yaml`：由 list 生成（勿手改）
- `config/contracts.json`：合約快取
- `config/strategies.yaml`：策略清單（可覆蓋）
- `config/strategy_limits.yaml`：風控限制
- `config/order_adapter.yaml`：下單節流/斷路器
- `config/recorder.yaml`：WAL/ClickHouse
- `config/execution.yaml`：執行設定
- `config/risk.yaml`：風控全域設定

---

## 3. 環境變數（核心）

### 3.1 模式/環境
- `HFT_MODE=sim|live|replay`
- `HFT_ENV=dev|staging|prod`
- `HFT_SYMBOLS=2330,2317`（逗號分隔；會覆蓋 config）
- `SYMBOLS_CONFIG=config/symbols.yaml`
- `HFT_STRATEGY_CONFIG=<path>`（策略 runner 設定）

### 3.2 Metrics / Logging
- `HFT_PROM_PORT=9090`
- `HFT_PROM_ADDR=0.0.0.0`
- `LOG_LEVEL=INFO|DEBUG|...`

---

## 4. Shioaji / CA 相關

### 4.1 登入
- `SHIOAJI_API_KEY`
- `SHIOAJI_SECRET_KEY`
- `SHIOAJI_ACCOUNT`（可選）

### 4.2 CA（下單簽章）
- `SHIOAJI_PERSON_ID`
- `SHIOAJI_CA_PATH` 或 `CA_CERT_PATH`
- `SHIOAJI_CA_PASSWORD` 或 `CA_PASSWORD`
- `SHIOAJI_ACTIVATE_CA=1` 或 `HFT_ACTIVATE_CA=1`

### 4.3 行為 / 連線
- `SHIOAJI_CONTRACTS_TIMEOUT`（合約拉取 timeout, ms）
- `SHIOAJI_FETCH_CONTRACT=1|0`
- `SHIOAJI_SUBSCRIBE_TRADE=1|0`

---

## 5. Feed / Normalizer

### 5.1 基本
- `HFT_EVENT_MODE=tuple|event`（事件格式）
- `HFT_TS_ASSUME_TZ=Asia/Taipei`（Exchange TS 假定時區）
- `HFT_TS_MAX_LAG_S=<sec>`（local_ts 與 exch_ts 最大容許差，超過則夾住）
- `HFT_TS_SKEW_LOG_COOLDOWN_S=<sec>`（時戳偏差告警的冷卻時間）

### 5.2 合成資料 / 偵錯
- `HFT_MD_SYNTHETIC_SIDE=1|0`
- `HFT_MD_SYNTHETIC_TICKS=<n>`
- `HFT_MD_LOG_RAW=1|0`
- `HFT_MD_LOG_EVERY=<n>`
- `HFT_MD_LOG_NORMALIZED=1|0`
- `HFT_MD_LOG_NORMALIZED_EVERY=<n>`

### 5.3 Reconnect / Resubscribe
- `HFT_RESUBSCRIBE_COOLDOWN=<sec>`
- `HFT_MD_RESUBSCRIBE_GAP_S=<sec>`
- `HFT_MD_RECONNECT_GAP_S=<sec>`
- `HFT_MD_FORCE_RECONNECT_GAP_S=<sec>`
- `HFT_MD_RECONNECT_COOLDOWN_S=<sec>`
- `HFT_MD_THREAD_OFFLOAD=1|0`（resubscribe/reconnect/snapshot 使用 to_thread）
- `HFT_RECONNECT_BACKOFF_S=<sec>`
- `HFT_RECONNECT_BACKOFF_MAX_S=<sec>`
- `HFT_RECONNECT_DAYS=mon,tue,...`
- `HFT_RECONNECT_HOURS=HH:MM-HH:MM`
- `HFT_RECONNECT_HOURS_2=HH:MM-HH:MM`
- `HFT_RECONNECT_TZ=Asia/Taipei`

### 5.4 Contract / Symbol 相關
- `HFT_ALLOW_SYMBOL_FALLBACK=1|0`
- `HFT_ALLOW_SYNTHETIC_CONTRACTS=1|0`
- `HFT_ALLOW_TRUNCATE_SUBSCRIPTIONS=1|0`
- `HFT_INDEX_EXCHANGE=TSE|OTC|...`
- `HFT_SYMBOL_METRICS=<path>`

### 5.5 Shioaji API Rate Limit (probe / internal)
- `HFT_SHIOAJI_API_SOFT_CAP`
- `HFT_SHIOAJI_API_HARD_CAP`
- `HFT_SHIOAJI_API_WINDOW_S`

---

## 6. Event Bus / Rust Accel
- `HFT_RUST_ACCEL=1|0`
- `HFT_BUS_RUST=1|0`
- `HFT_BUS_WAIT_MODE=event|spin`
- `HFT_BUS_SINGLE_WRITER=1|0`
- `HFT_BUS_NOTIFY_EVERY=<n>`
- `HFT_BUS_SPIN_SLEEP=<sec>`
- `HFT_BUS_SPIN_BUDGET=<n>`
- `HFT_BUS_BATCH_SIZE=<n>`

---

## 7. LOB / Metrics 相關
- `HFT_LOB_LOCKS=1|0`
- `HFT_LOB_READ_LOCKS=1|0`
- `HFT_LOB_LOCAL_TS=1|0`
- `HFT_LOB_STATS_MODE=event|poll`
- `HFT_LOB_FORCE_NUMPY=1|0`
- `HFT_METRICS_ENABLED=1|0`
- `HFT_METRICS_BATCH=<n>`
- `HFT_METRICS_ASYNC=1|0`

---

## 8. Order Adapter / API 防護
- `HFT_API_TIMEOUT_S`
- `HFT_API_GUARD_TIMEOUT_S`
- `HFT_API_MAX_INFLIGHT`
- `HFT_API_QUEUE_MAX`
- `HFT_API_COALESCE_WINDOW_S`

---

## 9. Queue / 系統行為
- `HFT_RAW_QUEUE_SIZE`
- `HFT_RAW_EXEC_QUEUE_SIZE`
- `HFT_RISK_QUEUE_SIZE`
- `HFT_ORDER_QUEUE_SIZE`
- `HFT_RECORDER_QUEUE_SIZE`
- `HFT_RECORDER_DROP_ON_FULL=1|0`
- `HFT_LOG_FILLS=1|0`
- `HFT_RUST_POSITIONS=1|0`

---

## 10. Recorder / ClickHouse
- `HFT_CLICKHOUSE_ENABLED=1|0`
- `HFT_DISABLE_CLICKHOUSE=1`（強制關閉）
- `HFT_CLICKHOUSE_HOST` / `CLICKHOUSE_HOST`
- `HFT_CLICKHOUSE_PORT` / `CLICKHOUSE_PORT`

---

## 11. Base Settings 範例（`config/base/main.yaml`）
```yaml
mode: sim
symbols:
  - "2330"
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

---

## 12. Symbols Workflow
```bash
hft config preview
hft config validate
hft config build --list config/symbols.list --output config/symbols.yaml
hft config sync --list config/symbols.list --output config/symbols.yaml
```

---

## 13. 策略限制 / 風控 / Recorder 範例
參考以下檔案：
- `config/strategy_limits.yaml`
- `config/risk.yaml`
- `config/order_adapter.yaml`
- `config/recorder.yaml`
- `config/execution.yaml`

---

## 14. 小結
- `.env` 是**最常用**的覆蓋方式
- `config/settings.py` 適合個人/本機實驗
- `config/env/<mode>` / `config/env/<env>` 適合部署

更多細節：`docs/getting_started.md`
