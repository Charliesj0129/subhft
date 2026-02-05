# Feed Adapter Guide

Feed Adapter 負責把 Shioaji 行情轉成標準事件，餵給 Event Bus / LOB。

---

## 1) 結構
- `feed_adapter/shioaji_client.py`：Shioaji login/subscribe/reconnect
- `feed_adapter/normalizer.py`：raw payload → `TickEvent` / `BidAskEvent`
- `feed_adapter/lob_engine.py`：LOB 更新 + `LOBStatsEvent`

---

## 2) Symbols 與訂閱

唯一來源：`config/symbols.list`
```bash
uv run hft config build --list config/symbols.list --output config/symbols.yaml
```

Docker Compose 預設 `SYMBOLS_CONFIG=config/base/symbols.yaml`。如需用自建 symbols：
```bash
# .env
SYMBOLS_CONFIG=config/symbols.yaml
```

---

## 3) Shioaji Simulation / Live
- `HFT_MODE=sim` → Shioaji simulation mode
- `HFT_MODE=live` → 真實帳務

CA 啟用：
```bash
export SHIOAJI_PERSON_ID=...
export SHIOAJI_CA_PATH=/path/to/Sinopac.pfx
export SHIOAJI_CA_PASSWORD=...
export SHIOAJI_ACTIVATE_CA=1
```

---

## 4) Reconnect / Resubscribe
常用環境變數：
- `HFT_RESUBSCRIBE_COOLDOWN`
- `HFT_MD_RECONNECT_GAP_S`
- `HFT_MD_FORCE_RECONNECT_GAP_S`
- `HFT_MD_RECONNECT_COOLDOWN_S`
- `HFT_RECONNECT_DAYS` / `HFT_RECONNECT_HOURS`

---

## 5) Metrics
- `feed_events_total`
- `feed_latency_ns`
- `feed_interarrival_ns`
- `feed_last_event_ts`
- `normalization_errors_total`
- `lob_updates_total`

---

## 6) LOB Engine

- Rust fast path: `HFT_RUST_ACCEL=1`（default）
- 強制 numpy path: `HFT_LOB_FORCE_NUMPY=1`
- 鎖與一致性：`HFT_LOB_LOCKS=1`, `HFT_LOB_READ_LOCKS=1`

`LOBStatsEvent` 包含：
- mid_price / spread / imbalance
- best_bid / best_ask
- bid_depth / ask_depth

---

## 7) 資料格式

### TickEvent
- price 是整數（scaled）
- exchange ts + local ts

### BidAskEvent
- bids/asks 為 numpy array（shape = [N, 2]）

---

## 8) 常見問題
- 看不到行情：確認 `SYMBOLS_CONFIG` 指向正確 `symbols.yaml`
- 跨週斷線：檢查 `HFT_RECONNECT_*` 與主機時間

