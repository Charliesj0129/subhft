# HFT Platform Runbooks

本文件提供值班與日常運維的標準處置流程。

## 1) Feed Gap / 無行情

徵兆：
- `feed_events_total` 停滯
- `feed_last_event_ts` 長時間不更新

檢查：
```bash
curl -fsS http://localhost:9090/metrics | rg "feed_events_total|feed_last_event_ts"
docker compose logs --tail=200 hft-engine
```

處置：
1. 檢查 `SYMBOLS_CONFIG` 是否正確。
2. 檢查 `HFT_QUOTE_NO_DATA_S`、`HFT_QUOTE_WATCHDOG_S` 設定。
3. 必要時重啟引擎：
```bash
docker compose restart hft-engine
```

## 2) Shioaji API latency 激增

檢查：
```bash
curl -fsS http://localhost:9090/metrics | rg "shioaji_api_latency_ms"
uv run python scripts/latency/shioaji_api_probe.py --mode sim --iters 30
```

處置：
- 檢查網路抖動與封包遺失。
- 視需要調整 `HFT_API_MAX_INFLIGHT`、`HFT_API_QUEUE_MAX`。

## 3) ClickHouse 連不上（DNS/啟動序）

症狀：
- log 出現 `NameResolutionError(host='clickhouse')`

處置：
```bash
docker compose up -d clickhouse redis
docker compose ps clickhouse
# 確認 healthy 後

docker compose restart hft-engine
```

## 4) ClickHouse `MEMORY_LIMIT_EXCEEDED`

症狀：
- `Insert failed, retrying with backoff`
- `MEMORY_LIMIT_EXCEEDED`

檢查：
```bash
docker compose logs --tail=300 hft-engine | rg -i "MEMORY_LIMIT_EXCEEDED|Insert failed"
docker compose logs --tail=200 clickhouse
```

處置：
1. 先確認是否可自動恢復（觀察是否出現 `Inserted batch`）。
2. 若持續發生：降低 ingest 壓力、調整 ClickHouse 記憶體與 merge 參數。
3. 需要時先維持 WAL（避免資料遺失）。

## 5) Recorder/WAL 堆積

檢查：
```bash
uv run hft recorder status
ls -lh .wal | head
```

處置：
1. 確認 ClickHouse 已連線。
2. 啟動/重啟 loader：
```bash
docker compose up -d wal-loader
```
3. 持續監控 backlog 是否下降。

## 6) Queue Depth 爆增 / Event Loop Lag

檢查：
```bash
curl -fsS http://localhost:9090/metrics | rg "queue_depth|event_loop_lag_ms"
```

處置：
- 調整 queue 容量（`HFT_*_QUEUE_SIZE`）。
- 檢查策略是否有阻塞 I/O。

## 7) 風控拒單 / 下單失敗

檢查：
```bash
curl -fsS http://localhost:9090/metrics | rg "risk_reject_total|order_reject_total"
docker compose logs --tail=200 hft-engine
```

處置：
- 檢查 `config/strategy_limits.yaml`、`config/risk.yaml`。
- 驗證策略輸入是否超限。

## 8) 時間偏移 / 未來時間資料

檢查：
```bash
date
timedatectl
docker exec hft-engine date
```

處置：
- 啟用 NTP/PTP。
- 確認 `HFT_TS_TZ`、`HFT_RECONNECT_TZ`。
