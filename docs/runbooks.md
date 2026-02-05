# HFT Platform Runbooks

本文件是運維/告警處理手冊，偏「實際操作」。

---

## 1) Feed Gap / 無行情

**徵兆**
- `feed_events_total` 停滯
- `feed_last_event_ts` 不更新
- ClickHouse `market_data` 沒有新資料

**檢查**
```bash
curl -s http://localhost:9090/metrics | rg "feed_events_total|feed_last_event_ts"
```

**操作**
1. 查看 hft-engine log：
   ```bash
   docker compose logs -f --tail=200 hft-engine
   ```
2. 檢查系統時間/時區（避免時間漂移）：
   ```bash
   date
   timedatectl
   ```
3. 若是 Shioaji 連線異常，重啟引擎：
   ```bash
   docker compose restart hft-engine
   ```
4. 若是週末/跨週斷線，確認 `HFT_RECONNECT_*` 設定。

---

## 2) Shioaji API Latency 激增

**徵兆**
- 下單/更新/取消耗時突然上升
- `shioaji_api_latency_ms` p95/p99 明顯上升

**量測**
```bash
uv run python scripts/latency/shioaji_api_probe.py --mode sim --iters 30
```

**處理**
- 檢查網路（RTT、封包遺失）
- 降低呼叫頻率 / 增加 coalesce window
- 檢查 `HFT_API_MAX_INFLIGHT` / `HFT_API_QUEUE_MAX`

---

## 3) Recorder/ClickHouse 失敗

**徵兆**
- `recorder_failures_total` 增加
- WAL 累積無法回灌

**檢查**
```bash
docker exec clickhouse clickhouse-client --query "SELECT count() FROM hft.market_data"
```

**處理**
1. 檢查 clickhouse container 狀態
2. 確認磁碟空間
3. WAL 回灌：
   ```bash
   sudo ./ops.sh replay-wal
   ```

---

## 4) Queue Depth 爆增 / Event Loop Lag

**徵兆**
- `queue_depth{queue=...}` 持續升高
- `event_loop_lag_ms` 超過門檻

**處理**
- 確認是否有同步 I/O
- 調整批次：`HFT_BUS_BATCH_SIZE`
- 若 metrics 本身太重，可關閉：`HFT_METRICS_ENABLED=0` 或提高 `HFT_METRICS_BATCH`

---

## 5) 下單失敗 / 風控拒單

**徵兆**
- `risk_reject_total` 增加
- log 出現 `Order Rejected by Risk` 或 `Circuit Breaker`

**處理**
1. 檢查 `config/strategy_limits.yaml`
2. 查看 `risk_log` / `orders_log`（ClickHouse）
3. 降低策略發單速率

---

## 6) 時間偏移 / 未來時間

**徵兆**
- ClickHouse 顯示「未來日期」（如 2/4）

**原因**
- 主機/容器時間錯誤或時區偏移

**處理**
```bash
date
# 若在容器
# docker exec hft-engine date
```

---

## 7) 系統健康檢查

```bash
curl -s http://localhost:9090/metrics | head

docker compose ps
```

---

更多細節：`docs/troubleshooting.md`
