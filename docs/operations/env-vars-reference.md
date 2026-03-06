# HFT Platform 環境變數參考

本文件整理所有 `HFT_*` 環境變數，依子系統分組說明預設值、用途與調整建議。

## 1. 總覽 — 優先級鏈

設定值依以下順序合併（後者覆蓋前者）：

```
Base YAML (config/base/main.yaml)
  → Env YAML (config/env/{mode}/main.yaml)
    → config/settings.py
      → 環境變數 (HFT_*, CLICKHOUSE_*, REDIS_*, ...)
        → CLI 參數 (--mode, --symbols, ...)
```

環境變數始終優先於 YAML 設定；重啟後生效。

---

## 2. 核心執行環境

| 變數 | 預設值 | 用途 |
|---|---|---|
| `HFT_MODE` | `sim` | 執行模式：`sim` / `live` / `replay` |
| `HFT_SYMBOLS` | — | 逗號分隔的交易標的清單（覆蓋 YAML） |
| `HFT_STRICT_PRICE_MODE` | `0` | `1` = 拒絕 float 型別價格（強制 int 精度） |
| `HFT_QUOTE_VERSION` | `auto` | Quote 協議版本：`auto` / `v0` / `v1` |
| `HFT_QUOTE_VERSION_STRICT` | `0` | `1` = 禁止 watchdog 自動降版至 v0 |
| `HFT_MD_RECORD_DIRECT` | `1` | `0` = 所有 BidAsk/Tick 經由 bus 錄製（非直接路徑） |
| `HFT_RECORDER_DROP_ON_FULL` | `1` | `0` = recorder queue 滿時等待（背壓）；`1` = 丟棄 |
| `HFT_BUS_BATCH_SIZE` | `0` | >1 時使用 batch consumer，減少事件迴圈喚醒次數 |
| `SYMBOLS_CONFIG` | `config/symbols.yaml` | 交易標的設定檔路徑 |

---

## 3. 服務監督（Supervisor）

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_TASK_RESTART_BACKOFF_S` | `1.0` | 服務崩潰後初始重啟等待（秒） | 降低以加快恢復（最小 0.1） |
| `HFT_TASK_RESTART_BACKOFF_MAX_S` | `30.0` | 重啟等待上限（秒） | 不建議超過 60s |
| `HFT_SUPERVISOR_QUEUE_LOG_EVERY_S` | `30.0` | queue 深度日誌記錄頻率 | 除錯時縮短至 5s |

**Runbook 參考**: [Section 9 — Supervisor restart](../runbooks.md#9-service-task-crash-supervisor-restart)

---

## 4. Redis Session 管理

用於多執行個體下的 feed session 排他控制。

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_RUNTIME_ROLE` | `engine` | 執行角色：`engine` / `maintenance` / `monitor` / `wal_loader` | 非交易節點設為 `monitor` 或 `wal_loader` |
| `HFT_REDIS_HOST` | `redis` | Redis 主機名稱（優先於 `REDIS_HOST`） | 生產環境設為實際 Redis 地址 |
| `HFT_REDIS_PORT` | `6379` | Redis 埠號（優先於 `REDIS_PORT`） | — |
| `HFT_REDIS_PASSWORD` | — | Redis 密碼（優先於 `REDIS_PASSWORD`） | 使用 secret manager |
| `HFT_FEED_SESSION_OWNER_KEY` | `feed:session:owner` | Redis key 名稱（session lease） | 多叢集隔離時設不同前綴 |
| `HFT_FEED_SESSION_OWNER_TTL_S` | `300` | Session lease TTL（秒）；最小 30s | 縮短以加速故障切換偵測 |
| `HFT_RUNTIME_INSTANCE_ID` | `{HOSTNAME}:{PID}` | 執行個體唯一識別（自動生成） | 多主機部署時手動設為固定值 |
| `HFT_FEED_SESSION_PREFLIGHT_TIMEOUT_S` | `0.5` | Preflight Redis 連線逾時（秒） | 低延遲網路可縮短至 0.2s |
| `HFT_FEED_SESSION_STALE_TAKEOVER_TTL_S` | `0` | >0 時，TTL 低於此值的 stale lease 可被清除並接管 | 0 = 停用（安全預設） |

**Runbook 參考**: [Section 12 — Redis Session Lease 衝突](../runbooks.md#12-redis-session-lease-衝突)

---

## 5. Queue 容量

所有 queue 容量最小值為 1024；設定值低於最小值時自動套用最小值。重啟後生效。

| 變數 | 預設值 | Queue 用途 | 調整建議 |
|---|---|---|---|
| `HFT_RAW_QUEUE_SIZE` | `65536` | 市場資料攝取（feed → normalizer） | 高頻多標的時可增至 131072 |
| `HFT_RAW_EXEC_QUEUE_SIZE` | `8192` | 執行回報事件（Shioaji → router） | 一般不需調整 |
| `HFT_RISK_QUEUE_SIZE` | `4096` | 風控引擎 / Gateway intent | 高 OPS 策略時可增至 8192 |
| `HFT_ORDER_QUEUE_SIZE` | `2048` | 下單派送 | 一般不需調整 |
| `HFT_RECORDER_QUEUE_SIZE` | `16384` | 持久化（WAL/ClickHouse） | WAL-first 模式可縮小至 4096 |

**告警**: `queue_depth[raw]` 持續 >90% 容量 → 消費者落後，考慮增大或優化消費者。

**Runbook 參考**: [Section 6 — Queue Depth 爆增](../runbooks.md#6-queue-depth-爆增--event-loop-lag)

---

## 6. ClickHouse / WAL-Loader

### 6.1 ClickHouse 連線

| 變數 | 預設值 | 用途 |
|---|---|---|
| `HFT_CLICKHOUSE_HOST` | `clickhouse` | ClickHouse 主機（亦接受 `CLICKHOUSE_HOST`） |
| `HFT_CLICKHOUSE_PORT` | `9000` | ClickHouse native protocol 埠（亦接受 `CLICKHOUSE_PORT`） |
| `HFT_CLICKHOUSE_USER` | `default` | 用戶名（亦接受 `CLICKHOUSE_USERNAME`, `CLICKHOUSE_USER`） |
| `HFT_CLICKHOUSE_PASSWORD` | — | 密碼（亦接受 `CLICKHOUSE_PASSWORD`） |

### 6.2 連線重試

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_CONNECT_MAX_RETRIES` | `10` | CH 連線失敗最大重試次數 | 降低以縮短啟動等待 |
| `HFT_CONNECT_BASE_DELAY_S` | `5.0` | 初始重試間隔（秒） | — |
| `HFT_CONNECT_MAX_BACKOFF_S` | `300.0` | 重試最長等待（秒） | 降至 60s 加快恢復偵測 |

### 6.3 插入重試

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_INSERT_MAX_RETRIES` | `3` | INSERT 失敗最大重試次數 | CH 壓力高時降至 1 |
| `HFT_INSERT_BASE_DELAY_S` | `0.5` | INSERT 初始重試間隔（秒） | CH 壓力高時增至 2.0 |
| `HFT_INSERT_MAX_BACKOFF_S` | `5.0` | INSERT 重試最長等待（秒） | CH 壓力高時增至 30.0 |
| `HFT_CH_INSERT_POOL_SIZE` | `8` | CH insert worker pool 大小 | 壓力高但 CPU 足夠時可增大 |
| `HFT_CH_MAX_CONCURRENT_INSERTS` | `6` | 同時進行 insert 的上限 | CH 不穩時先降至 1-2 |
| `HFT_CH_INSERT_CHUNK_ROWS` | `0` | >0 時啟用 chunked insert（每批 row 數） | 大批次回補時可設 256/512 |

**Runbook 參考**: [Section 4 — ClickHouse MEMORY_LIMIT_EXCEEDED](../runbooks.md#4-clickhouse-memory_limit_exceeded), [ch-mv-pressure-tuning](../runbooks/ch-mv-pressure-tuning.md)

### 6.4 WAL Loader 運作

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_WAL_POLL_INTERVAL_S` | `1.0` | WAL 目錄輪詢間隔（秒） | 降至 0.5s 加速追回堆積 |
| `HFT_WAL_SIZE_WARNING_MB` | `100` | WAL 目錄大小告警閾值（MB） | 磁碟小時縮小至 50 |
| `HFT_WAL_SIZE_CRITICAL_MB` | `500` | WAL 目錄大小緊急閾值（MB） | 磁碟小時縮小至 200 |
| `HFT_WAL_LOADER_CONCURRENCY` | `4` | 並行 WAL 檔案處理數 | SSD 環境可增至 8 |
| `HFT_WAL_DEDUP_ENABLED` | `0` | `1` = 啟用 replay 去重護欄 | 僅在 replay 重複問題時啟用 |
| `HFT_WAL_STRICT_ORDER` | `0` | `1` = 強制時序排序寫入 | 啟用後吞吐量降低 |
| `HFT_WAL_USE_MANIFEST` | `1` | `0` = 停用 manifest 追蹤 | 不建議停用 |
| `HFT_LOADER_ASYNC` | `1` | `0` = 同步模式（除錯用） | — |
| `HFT_WAL_BATCH_MAX_ROWS` | `5000` | WAL replay 每批 rows 上限（writer path） | 小資源主機可下調 |

### 6.5 保留與清理

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_ARCHIVE_RETENTION_DAYS` | `14` | WAL archive 保留天數 | 磁碟不足時縮短至 7 |
| `HFT_DLQ_RETENTION_DAYS` | `7` | Dead Letter Queue 保留天數 | — |
| `HFT_DLQ_ARCHIVE_PATH` | — | DLQ 長期存檔路徑（選填） | — |
| `HFT_CORRUPT_RETENTION_DAYS` | `30` | 損壞檔案保留天數 | — |
| `HFT_TS_MAX_FUTURE_S` | `5` | 時間戳未來偏移容許（秒）；超過則拒絕 | — |

**Runbook 參考**: [Section 5 — WAL 堆積](../runbooks.md#5-recorderwal-堆積), [Section 13 — WAL 磁碟滿](../runbooks.md#13-wal-磁碟滿diskpressurelevel--3)

### 6.6 WAL 磁碟壓力保護

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_WAL_DISK_MIN_MB` | `500` | WAL 可用磁碟低於此值時觸發壓力保護 | 緊急排障可暫降至 100 |
| `HFT_WAL_DISK_PRESSURE_POLICY` | `drop` | `drop`=拒寫保護資料安全；`warn`=只告警 | 生產建議維持 `drop` |

**Runbook 參考**: [recorder-wal-disk-pressure](../runbooks/recorder-wal-disk-pressure.md), [Section 13 — WAL 磁碟滿](../runbooks.md#13-wal-磁碟滿diskpressurelevel--3)

---

## 7. 行情（Quote）適配器

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_QUOTE_NO_DATA_S` | `10` | 無 quote 資料判定為 stall 的閾值（秒） | 低流動性標的可放寬至 30s |
| `HFT_QUOTE_WATCHDOG_S` | `5` | watchdog 輪詢間隔（秒） | 縮短至 2s 加快偵測 |
| `HFT_QUOTE_CB_RETRY_S` | `5` | quote callback 重試間隔（秒） | — |
| `HFT_QUOTE_VERSION` | `auto` | Quote 協議版本（同 Section 2） | 已記錄於 Section 2 |
| `HFT_QUOTE_VERSION_STRICT` | `0` | 禁止 watchdog 自動降版（同 Section 2） | — |
| `HFT_API_MAX_INFLIGHT` | `16` | 下單 API 同時 in-flight 上限 | API 延遲升高時下調 |
| `HFT_API_QUEUE_MAX` | `1024` | 下單 API 佇列上限 | 佇列爆滿時排查風控/下單耗時 |
| `HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY` | `none` | contract refresh 後重訂閱策略：`none`/`diff`/`all` | 生產穩定期建議 `none` |

**Runbook 參考**: [Section 1 — Feed Gap](../runbooks.md#1-feed-gap--無行情), [Section 2 — Shioaji API latency](../runbooks.md#2-shioaji-api-latency-激增), [Section 14 — Quote Schema 不符](../runbooks.md#14-quote-schema-不符version-mismatch), [shioaji-contract-refresh-operations](../runbooks/shioaji-contract-refresh-operations.md)

---

## 8. Gateway & Feature Engine

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_GATEWAY_ENABLED` | `0` | `1` = 啟用 CE-M2 Order/Risk Gateway（多節點路由） | 單節點部署保持 `0` |
| `HFT_GATEWAY_HA_ENABLED` | `0` | `1` = 啟用 active/standby leader lease gating | 單節點維持 `0` |
| `HFT_GATEWAY_LEADER_LEASE_PATH` | `.state/gateway_leader.lock` | Gateway leader lease 檔案路徑 | 主備需共享同一路徑 |
| `HFT_GATEWAY_LEADER_LEASE_REFRESH_S` | `0.5` | Leader lease heartbeat 週期（秒） | 小於 0.5 可加速切換但增加 I/O |
| `HFT_FEATURE_ENGINE_ENABLED` | `0` | `1` = 啟用 Feature Engine（16 個 LOB 特徵） | 安全 rollout：先影子模式測試 |
| `HFT_FEATURE_ENGINE_BACKEND` | `python` | FeatureEngine 核心後端：`python`/`rust` | 先 shadow 驗證後再切 `rust` |
| `HFT_FEATURE_ENGINE_EMIT_EVENTS` | `1` | `0` = 停用 FeatureUpdateEvent 發送 | 僅在診斷性能時暫停 |
| `HFT_FEATURE_PROFILE_ID` | — | 指定 Feature Profile ID | — |
| `HFT_FEATURE_SHADOW_PARITY` | `0` | `1` = 啟用 primary/shadow parity 比對 | rollout 必開 |
| `HFT_FEATURE_SHADOW_BACKEND` | `auto` | shadow backend（空值自動反向選擇） | 建議明確設 `python`/`rust` |
| `HFT_FEATURE_SHADOW_SAMPLE_EVERY` | `64` | 每 N 筆做一次 shadow parity | 高流量時可調大 |
| `HFT_FEATURE_SHADOW_WARN_EVERY` | `100` | 每 N 次 mismatch 輸出告警 | 減少 log 噪音 |
| `HFT_FEATURE_SHADOW_ABS_TOL` | `0` | parity 絕對容忍誤差 | Rust/Python 漂移調查時調整 |
| `HFT_FEATURE_METRICS_SAMPLE_EVERY` | `policy-dependent` | feature metrics 採樣週期（minimal=16, balanced=4, debug=1） | debug 可設 1 |
| `HFT_FEATURE_LATENCY_SAMPLE_EVERY` | `policy-dependent` | feature latency 採樣週期（minimal=16, balanced=4, debug=1） | debug 可設 1 |
| `HFT_ORDER_MODE` | — | 下單模式：`sim` / `live`（覆蓋 shioaji.simulation） | — |
| `HFT_ORDER_SIMULATION` | — | `1` = 模擬模式（舊版相容） | 優先使用 `HFT_ORDER_MODE` |
| `HFT_ORDER_NO_CA` | `0` | `1` = 停用 CA 認證（sim 環境） | — |

**Runbook 參考**: [feature-plane-operations](../runbooks/feature-plane-operations.md), [feature-plane-shadow-canary-runbook](../runbooks/feature-plane-shadow-canary-runbook.md), [gateway-ha-failover](../runbooks/gateway-ha-failover.md)

---

## 9. 可觀測性

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_EXPOSURE_MAX_SYMBOLS` | `10000` | ExposureStore 最大標的數（記憶體基數上限） | 標的少時可縮小至 1000 |
| `HFT_RECORDER_MODE` | `direct` | `wal_first` = WAL-only 寫入路徑（CE-M3） | 生產環境建議 `wal_first` |
| `HFT_TS_TZ` | `Asia/Taipei` | 時區假設（無時區時間戳解析） | 跨區部署務必顯式設定 |
| `HFT_RECONNECT_TZ` | `Asia/Taipei` | reconnect 時段判斷時區 | 與 `HFT_TS_TZ` 保持一致 |
| `HFT_DIAG_TRACE_ENABLED` | `0` | `1` = 啟用事件決策 trace 採樣 | 僅事故期間啟用 |
| `HFT_DIAG_TRACE_SAMPLE_EVERY` | `100` | 每 N 事件採樣一筆決策 trace | 流量大時可提高 |

**Runbook 參考**: [Section 8 — 時間偏移](../runbooks.md#8-時間偏移--未來時間資料), [incident-diagnostics](../runbooks/incident-diagnostics.md)

---

## 快速參考：故障對照表

| 症狀 | 優先檢查 | Runbook |
|---|---|---|
| Feed 停滯 | `HFT_QUOTE_NO_DATA_S`, `HFT_QUOTE_VERSION` | [Section 1](../runbooks.md#1-feed-gap--無行情) |
| Redis 衝突 | `HFT_FEED_SESSION_OWNER_TTL_S`, `HFT_RUNTIME_INSTANCE_ID` | [Section 12](../runbooks.md#12-redis-session-lease-衝突) |
| WAL 磁碟滿 | `HFT_ARCHIVE_RETENTION_DAYS`, `HFT_WAL_SIZE_CRITICAL_MB` | [Section 13](../runbooks.md#13-wal-磁碟滿diskpressurelevel--3) |
| CH INSERT 失敗 | `HFT_INSERT_MAX_RETRIES`, `HFT_INSERT_BASE_DELAY_S` | [Section 4](../runbooks.md#4-clickhouse-memory_limit_exceeded) |
| Queue 爆滿 | `HFT_RAW_QUEUE_SIZE`, `HFT_RECORDER_QUEUE_SIZE` | [Section 6](../runbooks.md#6-queue-depth-爆增--event-loop-lag) |
| Supervisor 重啟風暴 | `HFT_TASK_RESTART_BACKOFF_MAX_S` | [Section 9](../runbooks.md#9-service-task-crash-supervisor-restart) |
| Quote schema 不符 | `HFT_QUOTE_VERSION`, `HFT_QUOTE_VERSION_STRICT` | [Section 14](../runbooks.md#14-quote-schema-不符version-mismatch) |
