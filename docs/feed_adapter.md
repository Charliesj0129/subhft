# Feed Adapter Guide (Multi-Broker)

Feed Adapter 負責把 Broker 行情轉成平台標準事件，並提供重連/回補機制。支援多 Broker 架構（Shioaji / Fubon）。

## 1) 模組結構

```
src/hft_platform/feed_adapter/
├── broker_registry.py      # Broker 註冊與工廠選擇
├── protocol.py             # BrokerProtocol 定義（runtime_checkable）
├── normalizer.py           # 行情正規化（broker-agnostic）
├── lob_engine.py           # LOB 更新與統計
├── subscription_state.py   # 訂閱狀態管理
├── shioaji_client.py       # Legacy 單檔（已分解至 shioaji/）
├── _base/                  # 共用 broker 基礎模組
├── shioaji/                # Shioaji 子套件
│   ├── facade.py           # ShioajiFacade（統一介面）
│   ├── session_runtime.py  # 登入、重連、session 生命週期
│   ├── quote_runtime.py    # 行情訂閱與 callback
│   ├── order_gateway.py    # 下單、取消、修改
│   ├── account_gateway.py  # 部位、保證金查詢
│   └── contracts_runtime.py # 合約解析
└── fubon/                  # Fubon 子套件
    ├── facade.py           # FubonFacade（統一介面）
    ├── session_runtime.py  # 登入、重連
    ├── quote_runtime.py    # 行情 WebSocket 訂閱
    ├── order_gateway.py    # 下單 API
    ├── account_gateway.py  # 帳務查詢
    └── contracts_runtime.py # 合約解析
```

## 1b) Broker 選擇

透過 `HFT_BROKER` 環境變數或 `config/base/main.yaml` 中的 `broker:` key 選擇 broker：

```bash
# 環境變數方式
HFT_BROKER=shioaji  # 預設
HFT_BROKER=fubon    # 切換至 Fubon

# YAML 方式
# config/base/main.yaml
broker: shioaji
```

Registry pattern：每個 broker 的 `__init__.py` 會自動向 `broker_registry.py` 註冊工廠。Bootstrap 依據 `HFT_BROKER` 值觸發 import。

## 1c) Fubon 設定

Fubon broker 需安裝 `fubon-neo` SDK：

```bash
pip install fubon-neo
```

必要環境變數：
- `HFT_FUBON_CERT_PATH`：Fubon API 憑證檔路徑
- `HFT_FUBON_ACCOUNT`：交易帳號
- `HFT_FUBON_PASSWORD`：帳號密碼（建議使用 secret manager）

## 2) Symbols 與訂閱
來源：`config/symbols.list`

```bash
uv run hft config build --list config/symbols.list --output config/symbols.yaml
```

compose 預設 `SYMBOLS_CONFIG=config/base/symbols.yaml`；要改用自建版請在 `.env` 設定：
```bash
SYMBOLS_CONFIG=config/symbols.yaml
```

## 3) 模式與帳密

### Broker 選擇
- `HFT_BROKER=shioaji|fubon`（預設 `shioaji`）

### 共用
- `HFT_MODE=sim|live`

### Shioaji
- `SHIOAJI_API_KEY`, `SHIOAJI_SECRET_KEY`
- `SHIOAJI_PERSON_ID` / `SHIOAJI_CA_PATH` / `SHIOAJI_CA_PASSWORD`（CA 選用）

### Fubon
- `HFT_FUBON_CERT_PATH`（API 憑證路徑）
- `HFT_FUBON_ACCOUNT`（交易帳號）
- `HFT_FUBON_PASSWORD`（帳號密碼）

## 4) Quote watchdog / reconnect
關鍵參數：
- `HFT_QUOTE_WATCHDOG_S`
- `HFT_QUOTE_NO_DATA_S`
- `HFT_QUOTE_FORCE_RELOGIN_S`
- `HFT_QUOTE_FLAP_WINDOW_S`
- `HFT_QUOTE_FLAP_THRESHOLD`
- `HFT_QUOTE_FLAP_COOLDOWN_S`
- `HFT_RECONNECT_DAYS`, `HFT_RECONNECT_HOURS`, `HFT_RECONNECT_TZ`

## 5) Quote 版本與 schema guard
- `HFT_QUOTE_VERSION=auto|v0|v1`
- `HFT_QUOTE_VERSION_STRICT=0|1`
- `HFT_QUOTE_SCHEMA_GUARD=0|1`
- `HFT_QUOTE_SCHEMA_GUARD_STRICT=0|1`

## 6) Normalizer / LOB
- `HFT_EVENT_MODE=tuple|event`
- `HFT_RUST_ACCEL=1|0`
- `HFT_MD_SYNTHETIC_SIDE=1|0`
- `HFT_MD_SYNTHETIC_TICKS=<n>`

LOB：
- `HFT_LOB_LOCKS`, `HFT_LOB_READ_LOCKS`
- `HFT_LOB_FORCE_NUMPY`

## 7) 主要 metrics
- `feed_events_total`
- `feed_last_event_ts`
- `normalization_errors_total`
- `shioaji_api_latency_ms`
- `shioaji_api_errors_total`

## 8) 常見問題
- 無行情：先檢查 `SYMBOLS_CONFIG` + `hft config validate`
- 啟動期 NameResolutionError：先起 `clickhouse` 再起 `hft-engine`
- metrics scrape 異常：確認 label 值為字串（避免序列化錯誤）
