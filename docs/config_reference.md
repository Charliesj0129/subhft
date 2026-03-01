# Configuration Reference

本文件整理目前程式碼實際使用的設定來源與主要環境變數。

## 1. 設定優先序（由低到高）
1. `config/base/main.yaml`
2. `config/env/<mode>/main.yaml`（`HFT_MODE`）
3. `config/env/<env>/main.yaml`（`HFT_ENV`）
4. `config/settings.py`（本機覆蓋）
5. 環境變數（`HFT_*` / `SHIOAJI_*`）
6. CLI 參數

實作：`src/hft_platform/config/loader.py`

## 2. 主要設定檔
- `config/base/main.yaml`
- `config/env/sim/main.yaml`
- `config/env/live/main.yaml`
- `config/env/dev|staging|prod/main.yaml`
- `config/symbols.list`（來源）
- `config/symbols.yaml`（產物）
- `config/contracts.json`（合約快取）
- `config/base/strategies.yaml`、`config/strategies.yaml`
- `config/base/strategy_limits.yaml`、`config/strategy_limits.yaml`
- `config/base/order_adapter.yaml`、`config/order_adapter.yaml`
- `config/risk.yaml`、`config/execution.yaml`、`config/recorder.yaml`
- `config/feature_profiles.yaml`

## 3. 核心環境變數

### 3.1 模式與入口
- `HFT_MODE=sim|live|replay`
- `HFT_ENV=dev|staging|prod`
- `HFT_SYMBOLS=2330,2317`
- `SYMBOLS_CONFIG=config/symbols.yaml`
- `HFT_PROM_PORT=9090`
- `HFT_PROM_ADDR=0.0.0.0`

### 3.2 Shioaji / 帳密 / CA
- `SHIOAJI_API_KEY`
- `SHIOAJI_SECRET_KEY`
- `SHIOAJI_PERSON_ID`
- `SHIOAJI_ACCOUNT`（可選）
- `SHIOAJI_CA_PATH` / `CA_CERT_PATH`
- `SHIOAJI_CA_PASSWORD` / `CA_PASSWORD`
- `SHIOAJI_ACTIVATE_CA=1` 或 `HFT_ACTIVATE_CA=1`

### 3.3 Quote / Feed 健康
- `HFT_QUOTE_VERSION=auto|v0|v1`
- `HFT_QUOTE_VERSION_STRICT=0|1`
- `HFT_QUOTE_WATCHDOG_S`
- `HFT_QUOTE_NO_DATA_S`
- `HFT_QUOTE_FORCE_RELOGIN_S`
- `HFT_QUOTE_FLAP_WINDOW_S`
- `HFT_QUOTE_FLAP_THRESHOLD`
- `HFT_QUOTE_FLAP_COOLDOWN_S`
- `HFT_RECONNECT_DAYS`
- `HFT_RECONNECT_HOURS`
- `HFT_RECONNECT_HOURS_2`
- `HFT_RECONNECT_TZ`

### 3.4 Feed/Normalizer
- `HFT_EVENT_MODE=tuple|event`
- `HFT_RUST_ACCEL=1|0`
- `HFT_MD_SYNTHETIC_SIDE=1|0`
- `HFT_MD_SYNTHETIC_TICKS=<n>`
- `HFT_TS_MAX_LAG_S`
- `HFT_TS_MAX_FUTURE_S`
- `HFT_TS_SKEW_LOG_COOLDOWN_S`

### 3.5 Queue 與系統容量
- `HFT_RAW_QUEUE_SIZE`
- `HFT_RAW_EXEC_QUEUE_SIZE`
- `HFT_RISK_QUEUE_SIZE`
- `HFT_ORDER_QUEUE_SIZE`
- `HFT_RECORDER_QUEUE_SIZE`
- `HFT_RECORDER_DROP_ON_FULL=1|0`

### 3.6 Recorder / ClickHouse / WAL
- `HFT_CLICKHOUSE_ENABLED=1|0`
- `HFT_DISABLE_CLICKHOUSE=1`（強制關閉）
- `HFT_CLICKHOUSE_HOST`
- `HFT_CLICKHOUSE_PORT`
- `HFT_CLICKHOUSE_USER`
- `HFT_CLICKHOUSE_PASSWORD`
- `HFT_RECORDER_MODE=direct|wal_first`
- `HFT_WAL_DIR`
- `HFT_WAL_BATCH_MAX_ROWS`
- `HFT_WAL_DISK_MIN_MB`
- `HFT_WAL_DISK_PRESSURE_POLICY=drop|write|halt`（依實作）

### 3.7 Feature Plane
- `HFT_FEATURE_ENGINE_ENABLED=0|1`
- `HFT_FEATURE_ENGINE_BACKEND=python|rust`
- `HFT_FEATURE_PROFILES_CONFIG=config/feature_profiles.yaml`
- `HFT_FEATURE_PROFILE_ID=<id>`
- `HFT_FEATURE_ROLLOUT_STATE_PATH=outputs/feature_rollout_state.json`

### 3.8 Diagnostics Trace
- `HFT_DIAG_TRACE_ENABLED=0|1`
- `HFT_DIAG_TRACE_SAMPLE_EVERY`
- `HFT_DIAG_TRACE_DIR=outputs/decision_traces`
- `HFT_DIAG_TRACE_MAX_BYTES`

### 3.9 Gateway / CE-M2/HA
- `HFT_GATEWAY_ENABLED`（由系統配置決定是否啟用）
- `HFT_GATEWAY_HA_ENABLED`
- `HFT_GATEWAY_LEADER_LEASE_PATH`
- `HFT_GATEWAY_LEADER_LEASE_REFRESH_S`
- `HFT_GATEWAY_METRICS`

## 4. symbols 工作流
```bash
hft config preview
hft config validate
hft config build --list config/symbols.list --output config/symbols.yaml
hft config sync --list config/symbols.list --output config/symbols.yaml
hft config contracts-status
```

## 5. Docker 常見對應
`docker-compose.yml` 會注入：
- `HFT_MODE`, `HFT_ORDER_MODE`, `HFT_ORDER_NO_CA`
- `SHIOAJI_*`
- `HFT_CLICKHOUSE_HOST=clickhouse`, `HFT_CLICKHOUSE_PORT`
- `HFT_PROM_PORT`

## 6. 設定檢查建議
```bash
uv run hft check --export json
uv run hft config validate
uv run hft config contracts-status
```

## 7. 備註
- `.env` 僅本機使用，不得提交。
- 若要列出完整 env 變數，請以原始碼為準（`rg "os.getenv\(" src/hft_platform`）。
