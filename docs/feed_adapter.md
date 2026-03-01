# Feed Adapter Guide

Feed Adapter 負責把 Shioaji 行情轉成平台標準事件，並提供重連/回補機制。

## 1) 模組結構
- `src/hft_platform/feed_adapter/shioaji_client.py`
- `src/hft_platform/feed_adapter/normalizer.py`
- `src/hft_platform/feed_adapter/lob_engine.py`

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
- `HFT_MODE=sim|live`
- `SHIOAJI_API_KEY`, `SHIOAJI_SECRET_KEY`
- `SHIOAJI_PERSON_ID` / `SHIOAJI_CA_PATH` / `SHIOAJI_CA_PASSWORD`（CA 選用）

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
