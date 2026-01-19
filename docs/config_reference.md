# Configuration Reference

本文件整理所有設定來源、環境變數與 config 檔案格式。

## 1. 設定優先序
由低到高：
1. `config/base/main.yaml`
2. `config/env/<mode>/main.yaml`（可選）
3. `config/settings.py`（可選）
4. 環境變數 `HFT_*`
5. CLI 參數

對應實作：`src/hft_platform/config/loader.py`

## 2. 環境變數（.env）
**交易/登入**
- `SHIOAJI_PERSON_ID`
- `SHIOAJI_PASSWORD`
- `SHIOAJI_API_KEY`
- `SHIOAJI_SECRET_KEY`

**模式與 symbols**
- `HFT_MODE=sim|live|replay`
- `HFT_SYMBOLS=2330,2317`（逗號分隔）
- `SYMBOLS_CONFIG=path/to/symbols.yaml`

**監控**
- `HFT_PROM_PORT=9090`

**ClickHouse**
- `HFT_CLICKHOUSE_ENABLED=0|1`
- `HFT_CLICKHOUSE_HOST=localhost`
- `HFT_CLICKHOUSE_PORT=8123`
- `HFT_DISABLE_CLICKHOUSE=1`

**其他**
- `GIT_COMMIT`（Recorder 會記錄）

## 3. Base Settings (`config/base/main.yaml`)
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
replay:
  start_date: null
  end_date: null
```

## 4. Symbols (`config/symbols.yaml`)
```yaml
symbols:
  - code: "2330"
    exchange: "TSE"
    price_scale: 10000
```

欄位說明：
- `code`: 標的代碼
- `exchange`: TSE/OTC/FUT
- `price_scale`: 價格縮放倍數（用於內部整數價格）
- `tick_size`: 可替代 `price_scale`（若提供，會推算 scale）

## 5. Strategy Limits (`config/strategy_limits.yaml`)
```yaml
global_defaults:
  max_notional: 10_000_000
  price_band_ticks: 20
  max_order_rate: 180
  storm_guard_pnl: -500_000

strategies:
  STRAT_001:
    max_notional: 5_000_000
    price_band_ticks: 10
    storm_guard_pnl: -100_000

storm_guard:
  warm_threshold: -200_000
  storm_threshold: -500_000
  halt_threshold: -1_000_000
```

## 6. Risk (`config/risk.yaml`)
```yaml
daily_loss_limit: 10000
kill_switch_enabled: true
max_position_lots: 10
```

## 7. Order Adapter (`config/order_adapter.yaml`)
```yaml
rate_limits:
  shioaji_soft_cap: 180
  shioaji_hard_cap: 250
  sliding_window_s: 10

coalescing:
  amend_window_ms: 5
  max_batch_size: 50

timeouts:
  ack_timeout_ms: 1000
  cancel_retry_limit: 1

circuit_breaker:
  failure_threshold: 5
  reset_timeout_s: 60

execution:
  default_account: "sim-account-01"
```

## 8. Execution (`config/execution.yaml`)
```yaml
execution:
  snapshot_interval_s: 1.0
  reconciliation:
    heartbeat_threshold_ms: 1000
    polling_limited: true
  broker:
    account_id: "sim-account-01"
```

## 9. Recorder (`config/recorder.yaml`)
```yaml
recorder:
  wal_dir: ".wal"
  tables:
    market_data:
      flush_rows: 5000
      flush_ms: 1000
      buffer_size: 100000
    orders_log:
      flush_rows: 100
      flush_ms: 200
    risk_log:
      flush_rows: 100
      flush_ms: 200
clickhouse:
  host: "localhost"
  port: 8123
  database: "default"
```

## 10. Optional Overrides
- `config/settings.py`: 程式化 override（`hft init` 會產生）。
- `config/env/<mode>/main.yaml`: 環境專用覆蓋檔。

## 11. 建議做法
- 先改 `config/base/main.yaml` 與 `config/symbols.yaml`
- 開發期間用 `config/settings.py` 做私有覆蓋
- 正式環境透過 `.env` 與 CI 注入變數
