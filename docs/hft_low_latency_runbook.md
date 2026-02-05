# HFT Low-Latency Deployment Runbook

本文件針對低延遲環境的 host tuning 與部署實務。

---

## 1) Host Tuning
```bash
sudo ./ops.sh tune
sudo ./ops.sh hugepages
```

可選：CPU 隔離（soft realtime）
```bash
sudo ./ops.sh isolate "python -m hft_platform.main"
```

---

## 2) ClickHouse Data Path

建議 ClickHouse/WAL 放在獨立資料盤：
```bash
export HFT_CH_DATA_ROOT=/mnt/data/clickhouse
sudo ./ops.sh setup
```

---

## 3) Docker Compose (Host)
```bash
docker compose up -d --build
```

---

## 4) 快速檢查
```bash
docker exec clickhouse clickhouse-client --query \
  "SELECT count(), min(toDateTime64(exch_ts/1e9,3)), max(toDateTime64(exch_ts/1e9,3)) FROM hft.market_data"
```

---

## 5) 時間同步
- 建議 NTP / PTP
- 時間漂移會影響 `ingest_ts` 與 latency 分佈

---

## 6) 版本更新
- 建議用 immutable image
- 更新：`docker compose pull && docker compose up -d`
