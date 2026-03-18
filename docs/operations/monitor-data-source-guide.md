# Monitor Data Source 操作指南

Signal Monitor TUI 支援多種資料來源組合，依據延遲需求與基礎設施選擇最適模式。

## 模式選擇矩陣 (Mode Selection)

| 場景 | `data_source` | `source` | 延遲 | 需要 | 適用情境 |
|------|--------------|----------|------|------|---------|
| 開發/回測 | `ch` | `clickhouse` | ~2s | ClickHouse | 本地開發、離線分析 |
| 盤中監控 (基本) | `auto` | `clickhouse` | ~2s (CH) / ~μs (SHM) | CH + (optional) SHM | 預設模式，自動偵測 SHM |
| 盤中監控 (低延遲) | `shm` | — | ~μs | Engine + SHM segment | 最低延遲，無歷史資料 |
| 盤中監控 (Redis) | `ch` | `redis` | ~10ms | Redis + Engine publisher | Redis-only，需 `HFT_MONITOR_LIVE_ENABLED=1` |
| 盤中監控 (混合) | `ch` | `hybrid` | ~10ms live / ~2s history | Redis + CH | Redis 即時 tick + CH 歷史 sparkline |
| 全功能混合 | `auto` | `hybrid` | ~μs live / ~2s history | SHM + Redis + CH | SHM 優先，Redis fallback，CH 歷史 |

## 環境變數參考 (Env Var Reference)

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `HFT_MONITOR_DATA_SOURCE` | `auto` | 傳輸層: `ch` / `shm` / `auto` |
| `HFT_MONITOR_SOURCE` | `clickhouse` | 後端模式: `clickhouse` / `redis` / `hybrid` |
| `HFT_MONITOR_LIVE_ENABLED` | `0` | `1` = Engine 端啟用 Redis publisher |
| `HFT_MONITOR_REDIS_HOST` | `localhost` | Redis 主機 |
| `HFT_MONITOR_REDIS_PORT` | `6379` | Redis 埠號 |
| `HFT_MONITOR_REDIS_PASSWORD` | (空) | Redis 密碼 |
| `HFT_MONITOR_HYBRID_BACKFILL_INTERVAL_S` | `30.0` | 混合模式下 CH 歷史回填間隔 (秒) |

## 延遲特性 (Latency Characteristics)

```
SHM snapshot    ─────── ~1-10 μs ──── 最快，Engine 寫入共享記憶體
Redis pub/sub   ─────── ~5-15 ms ──── 中等，經 Redis live cache
ClickHouse poll ─────── ~1-3 s ────── 最慢，HTTP 查詢 + 聚合
```

- **SHM**: 透過 `ShmRingBuffer` 直接讀取 Engine 寫入的共享記憶體段，零網路開銷。
- **Redis**: Engine 以 `MarketDataService` 發布 L1 tick 到 Redis ring buffer，Monitor 輪詢。
- **ClickHouse**: 查詢 `hft.market_data` 表，受 poll_interval_s 與查詢延遲影響。

## 各模式設定步驟 (Setup)

### 模式 1: ClickHouse Only (預設)

無需額外設定，確保 ClickHouse 可連線即可:

```bash
export HFT_CLICKHOUSE_HOST=localhost
uv run hft monitor  # 或 scripts/run_signal_monitor.sh
```

### 模式 2: SHM (最低延遲)

需要 Engine 與 Monitor 在同一台機器上:

```bash
export HFT_MONITOR_DATA_SOURCE=shm
uv run hft monitor
```

Engine 端會自動建立 SHM segment。若 segment 不存在，`auto` 模式會 fallback 到 CH。

### 模式 3: Redis Live Cache

1. Engine 端啟用 publisher:
```bash
export HFT_MONITOR_LIVE_ENABLED=1
export HFT_MONITOR_REDIS_HOST=localhost
uv run hft run sim
```

2. Monitor 端切換 source:
```bash
export HFT_MONITOR_SOURCE=redis
uv run hft monitor
```

### 模式 4: Hybrid (Redis + CH)

Redis 負責即時 tick，ClickHouse 負責 warmup 與 sparkline 歷史:

```bash
# Engine
export HFT_MONITOR_LIVE_ENABLED=1
uv run hft run sim

# Monitor
export HFT_MONITOR_SOURCE=hybrid
export HFT_MONITOR_HYBRID_BACKFILL_INTERVAL_S=30
uv run hft monitor
```

`hybrid_backfill_interval_s` 控制 CH 歷史資料回填頻率。設定較長間隔可降低 CH 查詢負載。

### 模式 5: Auto (SHM + CH fallback)

```bash
export HFT_MONITOR_DATA_SOURCE=auto
uv run hft monitor
```

自動偵測 SHM segment:
- 存在 → SHM 即時 + CH 歷史 (backfill)
- 不存在 → 純 CH 模式

## 疑難排解 (Troubleshooting)

### Monitor 顯示 "STALE" 狀態

- 檢查 `stale_threshold_s` (預設 6s)，若 poll_interval 較長需對應調高
- SHM 模式: 確認 Engine 正在運行且寫入 SHM segment
- Redis 模式: 確認 `HFT_MONITOR_LIVE_ENABLED=1` 且 Engine 有發布資料

### Redis 連線失敗

```bash
# 測試 Redis 連線
redis-cli -h $HFT_MONITOR_REDIS_HOST -p $HFT_MONITOR_REDIS_PORT ping
# 檢查 Engine publisher 是否寫入
redis-cli -h $HFT_MONITOR_REDIS_HOST keys "monitor:l1:*"
```

### SHM segment 找不到

- 確認 Engine 與 Monitor 在同機器
- 檢查 `/dev/shm/` 下是否有 `hft_*` 開頭的檔案
- `auto` 模式會自動 fallback，不會報錯

### ClickHouse 查詢慢

- 檢查 `hft.market_data` 表大小: 大量資料可能導致 poll 超時
- 調高 `poll_interval_s` 降低查詢頻率
- 確認 ClickHouse 有足夠記憶體 (參考 `--max_memory_usage` 設定)

### Hybrid 模式歷史資料缺失

- 確認 CH 有 warmup 期間的歷史資料
- 調低 `hybrid_backfill_interval_s` 增加回填頻率
- 檢查 CH 表的 TTL 設定是否已清除過舊資料
