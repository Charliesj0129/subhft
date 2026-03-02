# Runbook: Disk Crisis SOP（磁碟危機標準作業程序）

> **適用對象**：本機開發機 + 舊電腦接收機（透過 Tailscale/SSH 連線）
> **最後更新**：2026-03-02（源自實際生產事件）

---

## 事件背景

**2026-03-02** 舊電腦接收機發生磁碟滿（216 GB / 100%），導致：
- SSH 頻繁斷線 / 超時
- ClickHouse 無法寫入新資料
- 系統整體反應緩慢

**根本原因**：`system.trace_log`（104 GB）+ `system.text_log`（19 GB）無 TTL，自容器首次啟動後持續累積。ClickHouse 預設對 trace_level 日誌全量記錄，無任何保留期限制。

---

## 1. 診斷流程（4 層）

### 第 1 層：網路連線

```bash
# 本機 Tailscale 狀態
tailscale status

# Ping 測試（packet loss 和延遲）
ping -c 10 ${REMOTE_IP}

# SSH RTT 基準
time ssh ${REMOTE_USER}@${REMOTE_IP} "echo ok"
```

**判斷依據**：
- packet loss > 5% → Tailscale/網路問題
- SSH RTT 顯著高於 ping RTT → 遠端 CPU/I/O 過載

### 第 2 層：系統資源

```bash
ssh ${REMOTE_USER}@${REMOTE_IP} << 'EOF'
free -h          # 記憶體（available < 200MB = CRITICAL）
swapon --show    # Swap（> 80% = CRITICAL）
df -h            # 磁碟（Use% > 95% = CRITICAL）
uptime           # Load average
ps aux --sort=-%mem | head -15
dmesg | grep -i "oom\|out of memory" | tail -10
EOF
```

### 第 3 層：HFT 服務健康

```bash
ssh ${REMOTE_USER}@${REMOTE_IP} << 'EOF'
cd ${REMOTE_PROJECT_PATH}
docker compose ps
docker compose logs --tail=30 hft-engine
ls .wal/ | wc -l   # WAL 積壓（> 100 = WARN）
docker exec clickhouse clickhouse-client -q "SELECT 1"  # CK ping
EOF
```

### 第 4 層：磁碟深度分析

```bash
ssh ${REMOTE_USER}@${REMOTE_IP} << 'EOF'
# 找出大目錄
du -sh /home /var /opt /tmp 2>/dev/null | sort -rh

# Docker 佔用
docker system df

# ClickHouse 各資料表大小（關鍵！）
docker exec clickhouse clickhouse-client --query \
  "SELECT database, table, formatReadableSize(sum(bytes_on_disk)) AS size
   FROM system.parts WHERE active
   GROUP BY database, table ORDER BY sum(bytes_on_disk) DESC LIMIT 20"
EOF
```

---

## 2. 常見根本原因與對應處置

### 2a. ClickHouse 系統日誌膨脹（最常見）

**症狀**：`system.trace_log` 或 `system.text_log` 超過 10 GB

**立即處置**（依大小順序執行）：
```bash
# 大表（> 50 GB）需要先建 flag
docker exec clickhouse bash -c \
  "touch /var/lib/clickhouse/flags/force_drop_table && chmod 666 /var/lib/clickhouse/flags/force_drop_table"

docker exec clickhouse clickhouse-client \
  --max_table_size_to_drop=0 \
  --query "TRUNCATE TABLE system.trace_log"

# 清理 flag
docker exec clickhouse rm -f /var/lib/clickhouse/flags/force_drop_table

# 其他系統日誌（通常 < 50 GB，不需要 flag）
for tbl in text_log part_log query_log processors_profile_log query_views_log asynchronous_metric_log; do
  docker exec clickhouse clickhouse-client -q "TRUNCATE TABLE system.${tbl}"
done
```

**永久防護**：確認 `config/clickhouse_system_logs.xml` 已掛載到 ClickHouse 容器。若未掛載：
```bash
# 確認 docker-compose.yml 有以下 volume mount:
# - ./config/clickhouse_system_logs.xml:/etc/clickhouse-server/config.d/system_logs.xml

docker compose up -d clickhouse  # 重建容器以套用新 mount
docker exec clickhouse clickhouse-client -q \
  "SELECT name, value FROM system.server_settings WHERE name IN ('max_table_size_to_drop')"
# 應輸出 0（表示設定生效）
```

### 2b. Docker build cache 累積

**症狀**：`docker system df` 顯示 Build Cache > 5 GB

```bash
docker builder prune -f   # 安全：只刪 cache，不影響 running 容器
```

### 2c. Docker 未使用 volumes / images

```bash
docker image prune -f    # 只刪未被任何容器引用的 image
docker volume prune -f   # 只刪未掛載的 volume
```

**注意**：`docker system prune -a` 會刪除所有未使用資源（含 stopped 容器），使用前確認所有需要的容器都在 running 狀態。

### 2d. 大型 Parquet/Native 匯出檔堆積

**症狀**：`${REMOTE_PROJECT_PATH}/exports/` 或 `${REMOTE_PROJECT_PATH}/*.parquet` 超過 5 GB

```bash
# 先確認（read-only）
ls -lhS ${REMOTE_PROJECT_PATH}/exports/ | head -20
du -sh ${REMOTE_PROJECT_PATH}/*.parquet 2>/dev/null | sort -rh

# 拉取到本地後再考慮清除（需使用者授權）
rsync -avz --progress ${REMOTE_USER}@${REMOTE_IP}:${REMOTE_PROJECT_PATH}/exports/ ./remote_exports_backup/
```

---

## 3. 預防機制

### 3a. 程式碼層防護

| 機制 | 位置 | 說明 |
|------|------|------|
| CK 系統日誌 TTL | `config/clickhouse_system_logs.xml` | trace_log: 3天; text_log: Warning+3天; query_log: 7天 |
| CK drop size 無限制 | 同上 | `max_table_size_to_drop=0` 避免 TRUNCATE 失敗 |
| Prometheus 告警 | `config/monitoring/alerts/rules.yaml` | 磁碟 < 20% = WARN; < 10% = CRITICAL |

### 3b. Prometheus 告警規則（已加入）

```yaml
- alert: HostDiskSpaceCritical
  expr: (node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) < 0.10
  severity: critical

- alert: HostDiskSpaceWarn
  expr: (node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) < 0.20
  severity: warning

- alert: ClickHouseSystemLogSizeCritical
  expr: sum by (table) (clickhouse_table_parts_bytes{database="system", table=~"trace_log|text_log|..."}) > 10 * 1024^3
  severity: warning
```

> **注意**：`node_filesystem_avail_bytes` 需要 `node_exporter` 在遠端機器上運行。若尚未部署，磁碟告警不會觸發。

### 3c. 定期清理建議（每週）

```bash
# 在遠端機器上加入 cron
# 每週日凌晨 3 點清理 Docker build cache
0 3 * * 0 docker builder prune -f >> /var/log/docker_cleanup.log 2>&1
```

---

## 4. ClickHouse 系統日誌保留期參考

| 資料表 | 內容 | 建議 TTL | 流量 |
|--------|------|----------|------|
| `system.trace_log` | CPU stack traces | 3 天 | 極高（每 tick 採樣） |
| `system.text_log` | 內部 server log | Warning+, 3 天 | 高（可透過 level 降低） |
| `system.query_log` | SQL 查詢歷史 | 7 天 | 中 |
| `system.part_log` | Merge/mutation 操作 | 7 天 | 低 |
| `system.asynchronous_metric_log` | 系統指標快照 | 3 天 | 中 |
| `system.metric_log` | 每分鐘指標 | 3 天 | 中 |
| `system.processors_profile_log` | Query pipeline profile | 3 天 | 中 |

---

## 5. SSH 頻繁斷線排查

若 SSH 連線不穩，但 ping 正常（0% packet loss），通常是遠端 I/O 過載：

1. 確認磁碟使用率（`df -h`）→ 若滿，先釋放空間
2. 確認 I/O wait（`vmstat 1 3` 的 `wa` 欄位）→ > 30% 表示 I/O 瓶頸
3. Tailscale 是否走 relay：ping avg > 50ms 且無 packet loss = DERP relay 模式
   - 執行 `tailscale ping ${REMOTE_IP}` 嘗試建立直連

---

## 6. 快速參考：釋放空間排序

| 操作 | 安全性 | 典型釋放空間 |
|------|--------|------------|
| TRUNCATE system.trace_log | ✅ 僅刪 debug 日誌 | 50~150 GB |
| TRUNCATE system.text_log | ✅ 僅刪 debug 日誌 | 10~30 GB |
| docker builder prune -f | ✅ 不影響 running 容器 | 5~25 GB |
| docker image prune -f | ✅ 不影響 running 容器 | 5~30 GB |
| docker volume prune -f | ⚠️ 確認 running 後再執行 | 1~10 GB |
| 移除 exports/*.parquet | ⚠️ 先備份到本地 | 視情況 |
| TRUNCATE hft.market_data | ❌ 禁止（生產資料） | — |

---

*此 Runbook 由 Claude Code 根據 2026-03-02 實際生產事件自動生成。*
