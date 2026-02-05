# Troubleshooting

常見問題與排查方向（依實際程式行為整理）。

---

## 1) run live 時自動降級 sim
**原因**：缺 `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY`。

**解法**
```bash
export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
export HFT_MODE=live
```

---

## 2) 無行情 / LOB 空白
**可能原因**
- `SYMBOLS_CONFIG` 指向錯誤
- `symbols.yaml` 未生成或與 `symbols.list` 不一致
- 交易所合約不存在 / 訂閱失敗

**排查**
```bash
uv run hft config preview
uv run hft config validate

curl -s http://localhost:9090/metrics | rg feed_events_total
```

---

## 3) 今天 2/3 卻看到 2/4 的資料
**原因**：
- 主機/容器時間或時區偏移
- `ingest_ts` 以 `time.time_ns()` 為基準，若系統時間走快會寫入「未來」

**排查**
```bash
date
# 容器內
# docker exec hft-engine date
```

**建議**
- 啟用 NTP/PTP 時間同步
- 在 ClickHouse query 中用 `toDateTime64(..., 'Asia/Taipei')` 比對

---

## 4) ClickHouse 寫入失敗 / WAL 堆積
**排查**
```bash
docker exec clickhouse clickhouse-client --query "SELECT count() FROM hft.market_data"
```

**修復**
- 檢查磁碟空間
- 重啟 `wal-loader`
- 使用 `sudo ./ops.sh replay-wal`

---

## 5) Metrics 無法連線
**排查**
- `HFT_PROM_PORT` 是否一致
- `hft-engine` 是否啟動

```bash
curl -s http://localhost:9090/metrics | head
```

---

## 6) 下單沒有送出
**可能原因**
- 風控拒單（`risk_reject_total`）
- Circuit breaker 開啟
- API rate limit / queue 滿

**排查**
- 查 log
- 檢查 `config/strategy_limits.yaml`
- 調整 `HFT_API_*` 或 `config/order_adapter.yaml`

---

## 7) Latency 突增
**排查**
- `event_loop_lag_ms` / `queue_depth`
- Shioaji API probe：
  ```bash
  uv run python scripts/latency/shioaji_api_probe.py --mode sim --iters 30
  ```

---

## 8) 測試不穩定
**排查**
```bash
uv run pytest -k <name> -vv
```

---

更多細節：
- `docs/runbooks.md`
- `docs/observability_minimal.md`
