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
| `HFT_INTENT_RECORDER_ENABLED` | `0` | `1` = 啟用 OrderIntent 錄製至 `hft.order_intents`（Slice C replay-parity gate 證據來源） |
| `HFT_KILL_LEDGER_ENABLED` | `1` | `1` 時 `promote_alpha()` 會在 Gate-C 拋例外或 Gate-D 拒絕時寫入 `audit.alpha_kill_ledger`；設 `0` 可關閉自動寫入（除錯 / operator dry-run） |
| `HFT_ALPHA_KILL_LEDGER_PATH` | `research/alphas/_kill_ledger.jsonl` | 覆寫離線 kill-ledger jsonl sink 路徑（測試用；生產環境不應設定） |
| `HFT_BUS_BATCH_SIZE` | `0` | >1 時使用 batch consumer，減少事件迴圈喚醒次數 |
| `HFT_BROKER` | `shioaji` | Broker 後端選擇：`shioaji` / `fubon` |
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
| `HFT_IMAGE` | `hft-platform:latest` | docker-compose 使用的 hft-engine image tag（`docker-compose.yml:13`） | 部署特定版本時設為 image:tag；rollback 時 `unset` 回 latest |

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
| `HFT_CLICKHOUSE_ENABLED` | `1` | `0` = 停用 ClickHouse 寫入（僅 WAL）；`1` = 啟用 |
| `HFT_CLICKHOUSE_HOST` | `clickhouse` | ClickHouse 主機（亦接受 `CLICKHOUSE_HOST`） |
| `HFT_CLICKHOUSE_PORT` | `8123` | ClickHouse HTTP 埠（`clickhouse-connect` 預設）；native protocol 走 `9000`（亦接受 `CLICKHOUSE_PORT`） |
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
| `HFT_WAL_RETENTION_DAYS` | `7` | WAL 檔案自動清理天數 | 磁碟空間不足時縮短 |
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
| `HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY` | `diff` | contract refresh 後重訂閱策略：`none`/`diff`/`all` | 預設 `diff` 確保 rollover 日自動轉訂新月合約；遇 broker 回傳異常合約清單（runbook Mode 2）時臨時改 `none` |

**Runbook 參考**: [Section 1 — Feed Gap](../runbooks.md#1-feed-gap--無行情), [Section 2 — Shioaji API latency](../runbooks.md#2-shioaji-api-latency-激增), [Section 14 — Quote Schema 不符](../runbooks.md#14-quote-schema-不符version-mismatch), [shioaji-contract-refresh-operations](../runbooks/shioaji-contract-refresh-operations.md)

---

## 8. Gateway & Feature Engine

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_GATEWAY_ENABLED` | `0` | `1` = 啟用 CE-M2 Order/Risk Gateway（多節點路由） | 單節點部署保持 `0` |
| `HFT_GATEWAY_HA_ENABLED` | `0` | `1` = 啟用 active/standby leader lease gating | 單節點維持 `0` |
| `HFT_GATEWAY_LEADER_LEASE_PATH` | `.state/gateway_leader.lock` | Gateway leader lease 檔案路徑 | 主備需共享同一路徑 |
| `HFT_GATEWAY_LEADER_LEASE_REFRESH_S` | `0.5` | Leader lease heartbeat 週期（秒） | 小於 0.5 可加速切換但增加 I/O |
| `HFT_FEATURE_ENGINE_ENABLED` | `1` | `1` = 啟用 Feature Engine（v3 預設 27 個 LOB 特徵）；`0` = 停用 | 預設 on；停用請設 `HFT_FEATURE_ENGINE_ENABLED=0` |
| `HFT_FEATURE_ENGINE_BACKEND` | `python` | FeatureEngine 核心後端：`python`/`rust` | 先 shadow 驗證後再切 `rust` |
| `HFT_FUSED_NORMALIZER` | `0` | `1` = 啟用 Rust fused normalizer + LOB pipeline（單次 FFI 跨界） | rollout 前 shadow parity 驗證 |
| `HFT_FEATURE_ENGINE_EMIT_EVENTS` | `1` | `0` = 停用 FeatureUpdateEvent 發送 | 僅在診斷性能時暫停 |
| `HFT_FEATURE_PROFILE_ID` | — | 指定 Feature Profile ID | — |
| `HFT_FEATURE_SHADOW_PARITY` | `0` | `1` = 啟用 primary/shadow parity 比對 | rollout 必開 |
| `HFT_FEATURE_SHADOW_BACKEND` | `auto` | shadow backend（空值自動反向選擇） | 建議明確設 `python`/`rust` |
| `HFT_FEATURE_SHADOW_SAMPLE_EVERY` | `64` | 每 N 筆做一次 shadow parity | 高流量時可調大 |
| `HFT_FEATURE_SHADOW_WARN_EVERY` | `100` | 每 N 次 mismatch 輸出告警 | 減少 log 噪音 |
| `HFT_FEATURE_SHADOW_ABS_TOL` | `0` | parity 絕對容忍誤差 | Rust/Python 漂移調查時調整 |
| `HFT_FEATURE_METRICS_SAMPLE_EVERY` | `policy-dependent` | feature metrics 採樣週期（minimal=16, balanced=4, debug=1） | debug 可設 1 |
| `HFT_FEATURE_LATENCY_SAMPLE_EVERY` | `policy-dependent` | feature latency 採樣週期（minimal=16, balanced=4, debug=1） | debug 可設 1 |
| `HFT_ORDER_MODE` | `sim` | 下單模式：`sim` / `live` / `disabled`；`disabled` 為純行情且不建立下單 session | `live` 仍受 `HFT_MODE` 與 `HFT_LIVE_CONFIRM` 雙重鎖保護 |
| `HFT_ORDER_SHADOW_MODE` | `0` | `1` = 啟用 ShadowOrderSink，攔截 NEW/CANCEL/AMEND 並記錄 shadow order | shadow rollout 必須設 `1`；`HFT_ORDER_MODE=sim` 單獨不足以保證 shadow persistence |
| `HFT_ORDER_SIMULATION` | — | `1` = 模擬模式（舊版相容） | 優先使用 `HFT_ORDER_MODE` |
| `HFT_ORDER_NO_CA` | `0` | `1` = 停用 CA 認證（sim 環境） | — |

**Runbook 參考**: [feature-plane-operations](../runbooks/feature-plane-operations.md), [feature-plane-shadow-canary-runbook](../runbooks/feature-plane-shadow-canary-runbook.md), [gateway-ha-failover](../runbooks/gateway-ha-failover.md)

Shadow deployment note:
- 單節點 old-computer shadow 部署建議 `HFT_GATEWAY_ENABLED=0`。
- 若 `HFT_GATEWAY_ENABLED=1`，intent 會先經 `GatewayService`；目前 code path 可能在 gateway/risk/reduce-only 之前就被拒絕，且不一定會經過 `OrderAdapter.execute()` 的 shadow intercept。
- futures shadow 部署前務必確認風控 price cap (`max_price_cap`) 與實際合約價格量級相容，否則會出現大量 `PRICE_EXCEEDS_CAP` 拒單。

### 8.1 Platform Reduce-Only / Manual Re-Arm

以下條件會讓平台進入 `PLATFORM_REDUCE_ONLY`，阻止新的 opening orders：

- `feed_reconnect_unhealthy`
- `feed_reconnect_pending`
- `feed_reconnect_flapping`
- `queue_depth_exceeded`
- `rss_unhealthy`
- `redis_unhealthy`
- `wal_backlog_unhealthy`
- `clickhouse_unhealthy`

關鍵觀測指標：
- `platform_reduce_only_active`
- `manual_rearm_required{scope="platform"}`
- `autonomy_transitions_total{scope="platform",...}`

自動恢復（Auto-Recovery）：

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_PLATFORM_AUTO_RECOVERY_ENABLED` | `1` | `1` = feed 相關 reduce-only 觸發清除後自動恢復 | 保守環境可設 `0` 維持純手動 |
| `HFT_PLATFORM_AUTO_RECOVERY_COOLDOWN_S` | `60` | 所有觸發條件清除後等待秒數，確認穩定才恢復 | 過短可能造成 flapping |
| `HFT_RECORDER_DATA_LOSS_BOOT_GRACE_S` | `60` | 開機後此秒數內暫不 latch `recorder_data_loss`（2026-06-18 boot-latch 事故：開機瞬態 DATA_LOSS 誤觸永久 reduce-only/HALT） | `0` = 停用（回到立即 latch）；真實 data loss 超過窗口仍會 latch |
| `HFT_PLATFORM_REDUCE_ONLY_FEED_GAP_S` | `600` | Active-symbol-aware feed-gap 觸發 `feed_reconnect_unhealthy` 的秒數門檻；2026-04 由 120s 提升為 600s，配合 800 標的訂閱宇宙與 illiquid 期權容忍度 | 視訂閱集流動性調整；過低易在 night session 誤觸發 |

重點：
- `platform_reduce_only_active=1` 時，shadow 策略即使仍在產生 intents，也可能沒有任何 new order 能往下游流動。
- Shadow mode (`HFT_ORDER_SHADOW_MODE=1`) 繞過 reduce-only gate — shadow orders 無金融風險，不受 reduce-only 限制。
- 自動恢復僅適用於 feed 相關觸發（`feed_reconnect_unhealthy`, `feed_gap_exceeded`）。若有非 feed 觸發（如 reconciliation drift），自動恢復會被阻擋。
- `manual_rearm_required=1` 表示依賴問題解除後仍需人工 re-arm（除非自動恢復已啟用且所有觸發條件已清除）。

---

## 9. 可觀測性

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_EXPOSURE_MAX_SYMBOLS` | `10000` | ExposureStore 最大標的數（記憶體基數上限） | 標的少時可縮小至 1000 |
| `HFT_RECORDER_MODE` | `direct` | `wal_first` = WAL-only 寫入路徑（CE-M3） | 生產環境建議 `wal_first` |
| `HFT_TS_TZ` | `Asia/Taipei` | 時區假設（無時區時間戳解析） | 跨區部署務必顯式設定 |
| `HFT_RECONNECT_TZ` | `Asia/Taipei` | reconnect 時段判斷時區 | 與 `HFT_TS_TZ` 保持一致 |
| `HFT_RECONNECT_DAYS` | `mon,tue,wed,thu,fri` | 自動重連啟用日（逗號分隔星期縮寫） | 週末 / 假日設空字串停用 |
| `HFT_RECONNECT_HOURS` | `08:30-13:35` | 交易時段自動重連窗口 | 開盤前/收盤後不觸發 |
| `HFT_RECONNECT_HOURS_2` | — | 第二交易時段窗口（期貨夜盤等） | 無需時可留空 |
| `HFT_RECONNECT_COOLDOWN` | `60` | 重連冷卻秒數 | 避免頻繁重連 |
| `HFT_RECONNECT_BACKOFF_S` | `5` | 初始重連退避延遲（秒） | 指數退避起始值 |
| `HFT_RECONNECT_BACKOFF_MAX_S` | `120` | 最大重連退避延遲（秒） | 封頂避免過長等待 |
| `HFT_LOGIN_CONNLIMIT_RETRIES` | `2` | broker 連線數上限（451）拒絕登入時的退避重試次數；0 = 停用（立即 fail-closed） | 重啟競態（session 未釋放）的耐受度；見 2026-06-21/22 failed-attempts |
| `HFT_LOGIN_CONNLIMIT_BACKOFF_S` | `75` | 每次 451 退避重試前等待秒數（等 broker 釋放前一 session 的槽位） | 需大於 broker session 釋放窗口（約 60s） |
| `HFT_QUOTE_FLAP_THRESHOLD` | `5` | 報價閃爍偵測：窗口內最大閃爍次數 | 超過則暫停訂閱 |
| `HFT_QUOTE_FLAP_WINDOW_S` | `60` | 報價閃爍偵測窗口（秒） | 配合 threshold |
| `HFT_QUOTE_FLAP_COOLDOWN_S` | `300` | 報價閃爍冷卻（秒） | 冷卻後自動重新訂閱 |

> `HFT_QUOTE_FLAP_*` 系列變數共同控制報價閃爍偵測行為。
| `HFT_STORMGUARD_FEED_GAP_HALT_S` | `30` | 行情斷流觸發 HALT 門檻（秒） | 超過此值進入 HALT |
| `HFT_DIAG_TRACE_ENABLED` | `0` | `1` = 啟用事件決策 trace 採樣 | 僅事故期間啟用 |
| `HFT_DIAG_TRACE_SAMPLE_EVERY` | `100` | 每 N 事件採樣一筆決策 trace | 流量大時可提高 |
| `HFT_ALPHA_AUDIT_ENABLED` | `0` | `1` = 啟用 Alpha 審計模式（release gate 必須設置） | `release-first-ops-gate` 前必須設為 `1` |

**Runbook 參考**: [Section 8 — 時間偏移](../runbooks.md#8-時間偏移--未來時間資料), [incident-diagnostics](../runbooks/incident-diagnostics.md)

---

## 10. Broker 選擇與 Fubon 帳密

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_BROKER` | `shioaji` | Broker 後端：`shioaji` / `fubon` | 切換 broker 需重啟 |
| `HFT_FUBON_CERT_PATH` | — | Fubon API 憑證檔路徑 | 使用 Fubon 時必填 |
| `HFT_FUBON_ACCOUNT` | — | Fubon 交易帳號 | 使用 Fubon 時必填 |
| `HFT_FUBON_PASSWORD` | — | Fubon 帳號密碼 | 使用 secret manager |

---

## 11. Monitor（Live Signal TUI）

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_MONITOR_SOURCE` | `clickhouse` | Monitor 資料來源：`clickhouse` / `redis` / `hybrid` | hybrid 模式結合即時與歷史 |
| `HFT_MONITOR_LIVE_ENABLED` | `0` | `1` = 啟用 Redis live publisher（在 MarketDataService 中） | 需 Redis 可用 |
| `HFT_MONITOR_REDIS_HOST` | `localhost` | Monitor Redis 主機 | — |
| `HFT_MONITOR_REDIS_PORT` | `6379` | Monitor Redis 埠號 | — |
| `HFT_MONITOR_REDIS_PASSWORD` | — | Monitor Redis 密碼 | 使用 secret manager |
| `HFT_MONITOR_DATA_SOURCE` | `auto` | 資料源層級：`ch` / `shm` / `auto` | `auto` 自動偵測 SHM 可用性 |

---

## 12. 部署

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_ENGINE_IMAGE` | `hft-engine:latest` | Docker image for hft-engine（含 tag/SHA） | 部署時指定已知穩定版本 |
| `HFT_AUTO_FLATTEN_DISABLED` | `0` | `1` = 禁用 HALT 自動平倉（手動介入模式） | 事故排查時暫時設為 `1` |
| `HFT_LIVE_CONFIRM` | `0` | `1` = 確認 live 模式啟動（防止誤操作） | 生產環境設 `1` |
| `HFT_SESSION_GOVERNOR_ENABLED` | `0` | `1` = 啟用 session governor 自動管理 | 多帳號場景使用 |

---

## 13. Position Checkpoint & Startup Recovery

### 13.1 Checkpoint 檔案

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_CHECKPOINT_ENABLED` | `1` | `1` = 啟用週期性 position checkpoint 持久化 | 維持 `1`；崩潰恢復必要 |
| `HFT_CHECKPOINT_PATH` | `.runtime/position_checkpoint.json` | Position checkpoint 檔案路徑 | Docker volume mount |
| `HFT_POSITION_CHECKPOINT_PATH` | — | 備用 checkpoint 路徑（優先於 `HFT_CHECKPOINT_PATH`） | 向後相容 |

### 13.2 啟動 Reconciliation

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_STARTUP_RECON_ENABLED` | `1` | `1` = 啟動時與 broker 對帳，差異超過閾值則拒啟 | 維持 `1`；停用僅限 sim |
| `HFT_STARTUP_RECON_QTY_THRESHOLD` | `10` | 股票部位差異容忍上限（股） | 高頻策略可調低 |
| `HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD` | `2` | 期貨部位差異容忍上限（口） | 1 口策略可調至 `1` |

---

## 14. ClickHouse Data & Backup

### 14.1 資料根目錄

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_CH_DATA_ROOT` | `/var/lib/clickhouse` | ClickHouse 資料根目錄 | Docker volume mount 路徑 |

### 14.2 自動備份

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_BACKUP_ENABLED` | `0` | `1` = 啟用每日自動備份（`backup` service） | 生產環境設 `1` |
| `HFT_BACKUP_RETAIN_DAYS` | `30` | 每日備份保留天數 | 磁碟不足時縮短至 7-14 |
| `CH_BACKUP_PATH` | `./backups/clickhouse` | 備份 host volume 路徑 | 跨磁碟備份請指向獨立磁區 |

---

## 15. Telegram 通知

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_TELEGRAM_ENABLED` | `0` | `1` = 啟用 Telegram bot 推播（事故 / 平倉 / HALT） | 生產環境設 `1` |
| `HFT_TELEGRAM_BOT_TOKEN` | — | BotFather 取得的 token | 使用 secret manager；勿硬編 |
| `HFT_TELEGRAM_CHAT_ID` | — | 接收訊息的 chat id（可由 `getUpdates` 查得） | 群組 chat id 為負數 |

---

## 16. 認證 & Broker 帳密（敏感）

> ⚠️ 以下為敏感憑證，務必透過 secret manager 注入；切勿提交到 git。

| 變數 | 預設值 | 用途 |
|---|---|---|
| `SHIOAJI_API_KEY` | — | Shioaji REST API key |
| `SHIOAJI_SECRET_KEY` | — | Shioaji REST secret key |
| `SHIOAJI_PERSON_ID` | — | 證券身份證字號（部分 API 必填） |
| `SHIOAJI_ACTIVATE_CA` | `0` | `1` = 啟動 CA 憑證（live 期貨/選擇權必須） |
| `CA_CERT_PATH` | — | CA 憑證檔路徑（如 `./certs/Sinopac.pfx`） |
| `CA_PASSWORD` | — | CA 憑證密碼 |

**Runbook 參考**: [live-trading-activation-sop](../runbooks/live-trading-activation-sop.md)

---

## 17. Maker Realism / Backtest 校準（Slice B）

Slice B 引入 Maker 策略 backtest 的殘餘部位 MtM、佇列校準、成本不確定性 gate 與嚴格延遲 audit；以下兩個變數為 operator 介面的覆寫。

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_MAKER_MARK_METHOD` | `last_mid` | `MakerEngine` 對日終未 FIFO 殘餘部位的 mark 方法；Slice B `_compute_residual_mtm` 使用。允許值：`last_mid` / `worse_of_mid_last_trade` | 標準 equity-curve 慣例使用 `last_mid`；最後一筆 trade 與 mid 偏離大時改用 `worse_of_mid_last_trade` 取保守估計 |
| `HFT_QUEUE_CALIBRATION_TABLE_PATH` | `research/backtest/q_hat_data/<HFT_SYMBOLS_PRIMARY>_q_hat.parquet` | `QueueDepletionFill` 用於 `queue_fraction` 查找的校準 `QHatTable` parquet 路徑；未設置時走 `0.5` 常數 fallback | 校準窗口變更（regime shift / 換約）或非預設標的 mix 跑 backtest 時覆寫 |

**Runbook 參考**: [maker-realism-gate](../runbooks/maker-realism-gate.md)

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
