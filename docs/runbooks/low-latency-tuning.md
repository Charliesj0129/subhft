# HFT Low-Latency Deployment Runbook

本文件聚焦低延遲與低抖動部署的實務步驟。

## 1) Host Tuning
```bash
sudo ./ops.sh tune
sudo ./ops.sh hugepages
```

可選 CPU 隔離：
```bash
sudo ./ops.sh isolate "python -m hft_platform.main"
```

## 2) 儲存與資料路徑
建議 ClickHouse/WAL 使用獨立資料盤。

```bash
export HFT_CH_DATA_ROOT=/mnt/data/clickhouse
sudo ./ops.sh setup
```

## 3) Docker 啟動順序（降低啟動期錯誤）
```bash
docker compose up -d clickhouse redis

# 等 clickhouse healthy
docker compose ps clickhouse

docker compose up -d --build hft-engine
```

## 4) 快速驗證
```bash
curl -fsS http://localhost:9090/metrics | head

docker exec clickhouse clickhouse-client --query \
  "SELECT count() FROM hft.market_data"
```

## 5) 啟動後 5 分鐘觀察
```bash
docker compose logs --since=5m hft-engine | rg -i "error|Traceback|MEMORY_LIMIT_EXCEEDED|NameResolutionError"
```

## 6) 時間同步
- 啟用 NTP/PTP。
- 確保主機與容器時區一致（`Asia/Taipei`）。

## 7) 版本更新

本節適用於**新建或自用的調校主機**。

對既有生產主機（THESHOW）**不適用**：該主機工作樹永久分岔且帶有只存在於本機的
手動編輯，`git pull` 會毀掉它們；`up -d --build` 會 recreate 容器並丟掉
`/app/outputs`。生產發布一律依 `docs/runbooks/deployment.md`。

```bash
# 僅限調校/開發主機
git pull
docker compose up -d --build hft-engine
```
