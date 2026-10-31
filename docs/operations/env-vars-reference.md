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
| `HFT_STRICT_PRICE_MODE` | `1` | `1`（預設）= 策略以 float 下價時拋 `TypeError`，要求 int 縮放價或 `Decimal`；`0` = 只記 warning，並以 `Decimal(str(price))` 縮放（舊版相容，不建議）。`strategy/base.py:30`、`:103–114` |
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
| `HFT_GC_DISABLE_TRADING` | `0` | `1`／`true`／`yes`／`on` = 交易時段內 `gc.disable()`，log 為 `GC disabled for trading session`（`services/system.py:848–851`）。**名稱不是停用交易**，只關閉 Python 垃圾回收 |
| `HFT_CONFIG_STRICT` | `0` | `1` = 設定檔驗證用嚴格模式；`settings` 綁定 `loop_id` 時也一律嚴格（`config/loader.py:253`） |

---

## 3. 服務監督（Supervisor）

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_TASK_RESTART_BACKOFF_S` | `1.0` | 服務崩潰後初始重啟等待（秒） | 降低以加快恢復（最小 0.1） |
| `HFT_TASK_RESTART_BACKOFF_MAX_S` | `30.0` | 重啟等待上限（秒） | 不建議超過 60s |
| `HFT_SUPERVISOR_QUEUE_LOG_EVERY_S` | `30.0` | queue 深度日誌記錄頻率 | 除錯時縮短至 5s |
| `HFT_LOOP_STALL_KILL_S` | `60` | 事件迴圈超過此秒數沒有心跳，看門狗執行緒就 `os._exit(70)`，由容器重啟策略拉起引擎（`services/system.py:859`、`services/loop_watchdog.py`）。**`<=0` 停用整個看門狗**。`risk/storm_guard.py:410` 也讀同一個值來檢查延遲門檻 | 停用後迴圈卡死不會被自動重啟；`os._exit` 會跳過 `close()` 等清理 |
| `HFT_LOOP_STALL_CHECK_S` | `5` | 看門狗檢查間隔（秒，下限 0.1；`services/system.py:860`） | — |
| `HFT_LOOP_STALL_WARN_S` | `15` | 在 kill 之前提早傾印堆疊的門檻（秒）；`<=0` 只停用這次提早傾印，kill 時仍會傾印（`services/system.py:861`） | — |
| `HFT_MTM_INCOMPLETE_REPEAT_TICKS` | `3600` | mark-to-market 無法定價時，重覆提醒之間的 supervisor tick 數（1 Hz，故預設為每小時一次）；進入、標的變動、恢復三個邊緣一律立即記錄 | `0` = 只記錄邊緣，不重覆提醒 |

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
| `HFT_RAW_QUEUE_SIZE` | `65536 × HFT_QUOTE_CONNECTIONS`（程式碼）；compose `10000` | 市場資料攝取（feed → normalizer） | `docker-compose.yml:89` 與 `docker-compose.prod.locked.yml` 以 `${HFT_RAW_QUEUE_SIZE:-10000}` 覆蓋程式碼預設，`.env` 沒設時實際為 `10000`。高頻多標的時可增至 131072 |
| `HFT_RAW_EXEC_QUEUE_SIZE` | `8192` | 執行回報事件（Shioaji → router） | 一般不需調整 |
| `HFT_RISK_QUEUE_SIZE` | `4096` | 風控引擎 / Gateway intent | 高 OPS 策略時可增至 8192 |
| `HFT_ORDER_QUEUE_SIZE` | `2048` | 下單派送 | 一般不需調整 |
| `HFT_RECORDER_QUEUE_SIZE` | `16384 × HFT_QUOTE_CONNECTIONS` | 持久化（WAL/ClickHouse） | WAL-first 模式可縮小至 4096 |

預設值取自 `services/bootstrap.py:639–643`、`:783–789`：raw 與 recorder 兩條佇列的程式碼預設會乘上 `HFT_QUOTE_CONNECTIONS`。

**告警**: `queue_depth[raw]` 持續 >90% 容量 → 消費者落後，考慮增大或優化消費者。

**Runbook 參考**: [Section 6 — Queue Depth 爆增](../runbooks.md#6-queue-depth-爆增--event-loop-lag)

---

## 6. ClickHouse / WAL-Loader

### 6.1 ClickHouse 連線

| 變數 | 預設值 | 用途 |
|---|---|---|
| `HFT_CLICKHOUSE_ENABLED` | 程式碼 `0`；compose `1` | `1`／`true`／`yes`／`on` = 啟用 ClickHouse 寫入；**未設定或空字串視為停用**（`recorder/writer.py:109`、`recorder/worker.py:812`；`order/shadow_writer.py:52` 只認 `1`）。compose 在共用環境區塊固定寫 `HFT_CLICKHOUSE_ENABLED=1`（`docker-compose.yml:40`，不是 `${}` 形式，`.env` 蓋不掉） |
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
| `HFT_WAL_SIZE_WARNING_MB` | `100` | WAL 目錄大小告警閾值（MB） | 磁碟小時縮小至 50 |
| `HFT_WAL_SIZE_CRITICAL_MB` | `500` | WAL 目錄大小緊急閾值（MB） | 磁碟小時縮小至 200 |
| `HFT_WAL_LOADER_CONCURRENCY` | `4` | 並行 WAL 檔案處理數 | SSD 環境可增至 8 |
| `HFT_WAL_DEDUP_ENABLED` | `1` | 預設啟用 replay 去重護欄（`recorder/loader.py:135`）；`0` = 停用 | 停用後 replay 少了這層去重 |
| `HFT_WAL_STRICT_ORDER` | `1` | 預設強制 WAL 檔案時序排序（`recorder/loader.py:138`）；`0` = 不強制 | 原文件註記排序會降低吞吐量（未量測） |
| `HFT_WAL_USE_MANIFEST` | `1` | `0` = 停用 manifest 追蹤 | 不建議停用 |
| `HFT_WAL_MANIFEST_PATH` | `<wal_dir>/manifest.txt` | manifest 檔位置 | **設到 `.wal/` 之外**（例：`/app/.state/wal_manifest.txt`）。預設放在 `.wal/` 根目錄，而 §6.6 的磁碟壓力監測會把 manifest 自身的體積算成 WAL 壓力 — 2026-07-24 假 HALT 事故的成因 |
| `HFT_LOADER_ASYNC` | `1` | `0` = 同步模式（除錯用） | — |
| `HFT_WAL_BATCH_MAX_ROWS` | `5000` | WAL replay 每批 rows 上限（writer path） | 小資源主機可下調 |

> `HFT_WAL_RETENTION_DAYS` 在程式碼中沒有任何讀取者，設了不會有效果。WAL archive 的清理天數由 §6.5 的
> `HFT_ARCHIVE_RETENTION_DAYS`（預設 `14`，`recorder/loader.py:108`）控制。

### 6.5 保留與清理

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_ARCHIVE_RETENTION_DAYS` | `14` | WAL archive 保留天數 | 磁碟不足時縮短至 7 |
| `HFT_DLQ_RETENTION_DAYS` | `7` | Dead Letter Queue 保留天數 | — |
| `HFT_DLQ_ARCHIVE_PATH` | 程式碼：未設定；compose：`/app/.wal/dlq_expired` | 過期 DLQ 檔的封存目錄。**未設定時，過期的 DLQ 檔直接 `os.remove`**，設定後改為搬到此目錄（`recorder/loader.py:99`、`recorder/_loader_dlq.py:230–236`） | compose 預設為 `${HFT_DLQ_ARCHIVE_PATH:-/app/.wal/dlq_expired}`（`docker-compose.yml:114`）；`docker-compose.production.yml` 沒有設這個變數 |
| `HFT_CORRUPT_RETENTION_DAYS` | `30` | 損壞檔案保留天數 | — |
| `HFT_TS_MAX_FUTURE_S` | `5` | 時間戳未來偏移容許（秒）；超過則拒絕 | — |

**Runbook 參考**: [Section 5 — WAL 堆積](../runbooks.md#5-recorderwal-堆積), [Section 13 — WAL 磁碟滿](../runbooks.md#13-wal-磁碟滿diskpressurelevel--3)

### 6.6 WAL 磁碟壓力保護

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_WAL_DISK_MIN_MB` | `500` | WAL 可用磁碟低於此值時觸發壓力保護 | 緊急排障可暫降至 100 |
| `HFT_WAL_DISK_PRESSURE_POLICY` | `halt` | 可用磁碟低於 `HFT_WAL_DISK_MIN_MB` 時，`WALWriter` 與 `WALBatchWriter` 的斷路器丟棄該批 rows，記 warning 與 `recorder_wal_skipped_rows_total{reason="disk_full"}`。**只有 `raise` 會拋 `RuntimeError`；`halt`、`drop`、`warn` 與其他任何值行為相同，都是丟列**（`recorder/wal.py:52`、`:97–117`、`:284`、`:339–360`） | `warn` 並不是「只告警」，一樣丟列；`halt` 這個值本身不會觸發 HALT。但這是 ClickHouse 寫入失敗後的 WAL 後備路徑：丟列時 `WALWriter.write`／`WALBatchWriter.add` 回傳 `False`，`recorder/writer.py` 記 `Data loss: both ClickHouse and WAL failed` 與 `data_loss` 事件並拋 `WriterDoubleFaultError`（`recorder/writer.py:723–731`、`:882–890`），後果見下方「`data_loss` 的後果」。`hft recorder status` 顯示的預設是 `drop`（`cli/_ops.py:282`），和引擎的 `halt` 不同 |
| `HFT_WAL_FIRST_POLICY_<TABLE>` | `write` | 只在 `HFT_RECORDER_MODE=wal_first` 有效（`DiskPressureMonitor` 只在該模式建立，`recorder/worker.py:889–898`）。壓力達 CRITICAL（`HFT_WAL_CRITICAL_MB`）時依 table 設定：`write` 照常寫入；`drop` 與 `halt` 都是丟棄該批 rows。壓力達 HALT 等級時所有 table 一律丟棄，不看此設定。值不合法視為 `write`（`recorder/disk_monitor.py:99–105`、`recorder/wal_first.py:36–65`）。`<TABLE>` 是 table 名稱的大寫 | 丟棄的 rows 會記 `data_loss` 事件，使 recorder 健康度進入 `DATA_LOSS`（`recorder/worker.py:932–937`），後果見下方「`data_loss` 的後果」 |
| `HFT_WAL_WARN_MB` | `100` | `DiskPressureMonitor` WARN 閾值：`.wal/` 根目錄（maxdepth 1）總量（MB） | — |
| `HFT_WAL_CRITICAL_MB` | `300` | 同上，CRITICAL 閾值 | — |
| `HFT_WAL_HALT_MB` | `500` | 同上，HALT 閾值。**達到即開始丟棄 rows**，並 latch `recorder_data_loss` → PLATFORM_REDUCE_ONLY（非自動復原） | 除非確知磁碟更大，否則不要調高來「消除告警」——那等於拿掉資料保護 |

> `DiskPressureMonitor` 的 HALT 等級本身沒有任何 hook 消費者（`register_hook` 在 src 內沒有呼叫點，
> `recorder/disk_monitor.py:94`），所以這個等級自己不呼叫 `StormGuard`，只會讓 `WALFirstWriter` 丟列。
>
> **`data_loss` 的後果**：`WALFirstWriter` 丟列（`recorder/worker.py:932–937`）與上表 `HFT_WAL_DISK_PRESSURE_POLICY` 的 WAL 後備丟列（`recorder/writer.py:723–731`、`:882–890`）都會記 `data_loss` 事件；時間窗內有此事件，recorder 狀態即為 `DATA_LOSS`（`recorder/health.py:102–120`）。`PlatformDegradeInputs` 隨後回報 `recorder_data_loss`（開機寬限期內除外，`ops/platform_inputs.py:203–208`）；supervisor 對每個原因呼叫 `enter_reduce_only_async`（`services/system.py:1524–1525`），`autonomy_monitor` 在平台尚未 reduce-only 時另外呼叫 `StormGuard.trigger_halt("recorder_data_loss")`（`ops/autonomy_monitor.py:277–293`）。這個原因不在 `_AUTO_RECOVERABLE_REASONS`（`ops/platform_degrade.py:26–45`），需人工 re-arm。所以丟列不會停止引擎行程，但會讓平台進入 PLATFORM_REDUCE_ONLY，並可能進入 HALT。

> **不要和 §6.4 的 `HFT_WAL_SIZE_WARNING_MB` / `HFT_WAL_SIZE_CRITICAL_MB` 搞混。**
> 名稱幾乎一樣，消費者卻不同：`HFT_WAL_SIZE_*` 由 wal-loader（`recorder/loader.py`）
> 用於 backlog 告警；本節這三個由引擎的 `DiskPressureMonitor`
> （`recorder/disk_monitor.py`）使用，是唯一會真的**丟行情資料**的那組。調錯那組
> 不會有任何效果，而且問題會照舊發生。

**Runbook 參考**: [recorder-wal-disk-pressure](../runbooks/recorder-wal-disk-pressure.md), [Section 13 — WAL 磁碟滿](../runbooks.md#13-wal-磁碟滿diskpressurelevel--3)

---

## 7. 行情（Quote）適配器

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_QUOTE_NO_DATA_S` | `30` | 無 quote 資料判定為 stall 的閾值（秒）（`feed_adapter/shioaji/_config.py:276`、`client.py:480`；compose 同為 `30`，`docker-compose.yml:28`） | 低流動性標的可再放寬 |
| `HFT_QUOTE_WATCHDOG_S` | `5` | watchdog 輪詢間隔（秒） | 縮短至 2s 加快偵測 |
| `HFT_QUOTE_CB_RETRY_S` | `5` | quote callback 重試間隔（秒） | — |
| `HFT_QUOTE_VERSION` | `auto` | Quote 協議版本（同 Section 2） | 已記錄於 Section 2 |
| `HFT_QUOTE_VERSION_STRICT` | `0` | 禁止 watchdog 自動降版（同 Section 2） | — |
| `HFT_API_MAX_INFLIGHT` | `16` | 下單 API 同時 in-flight 上限 | API 延遲升高時下調 |
| `HFT_API_QUEUE_MAX` | `1024` | 下單 API 佇列上限 | 佇列爆滿時排查風控/下單耗時 |
| `HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY` | `diff` | contract refresh 後重訂閱策略：`none`/`diff`/`all` | 預設 `diff` 確保 rollover 日自動轉訂新月合約；遇 broker 回傳異常合約清單（runbook Mode 2）時臨時改 `none` |
| `HFT_QUOTE_CONNECTIONS` | `1` | Quote 連線池的 facade 數量；符號宇宙依此切分成各自的 shard | 生產（THESHOW）為 `4`。每個 facade 有自己的 session-refresh 執行緒與 watchdog，所以此值同時決定下列 §7.1 各旋鈕要序列化幾個登入 |
| `HFT_FACADE_RECONNECT_PRE_OPEN_LEAD_S` | `900` | facade 重連的開盤前提前量（秒）。已登出的 facade 在「盤中，或 `lead` 秒後即開盤」時才允許復原（`QuoteConnectionPool.reconnect_allowed`）；預設值對應 08:45 開盤前的 08:30 | 行事曆無法回答時一律放行（fail-open）。換月日的合約換月（universe roll）也在這個窗口前後被採用，見 [SymbolsYamlRegeneration](../runbooks/SymbolsYamlRegeneration.md) |
| `HFT_SYMBOLS_RUNTIME_SNAPSHOT` | `data/live_with_options.yaml` | 連線池 universe roll 寫出的執行期快照路徑（輸出檔）。從不寫入 `config/symbols.yaml`；若與輸入檔相同，會改用預設路徑或 `<輸入檔名>.runtime.yaml` 旁檔，兩者都撞名才拋 `RuntimeError` | 只有要把快照放到別處時才設；不要指向 `SYMBOLS_CONFIG` 所指的輸入檔 |

訂閱上限與合約後備（`feed_adapter/shioaji/client.py`）：

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_MAX_SUBSCRIPTIONS` | `600` | 每個 `ShioajiClient` 的訂閱上限（universe 上限，不是每條連線的 120 檔）。`shioaji_config.max_subscriptions_per_client` 有設時優先於此變數（`feed_adapter/shioaji/limits.py:38–55`、`client.py:255–257`） | — |
| `HFT_ALLOW_TRUNCATE_SUBSCRIPTIONS` | `1` | 標的數超過上限時，`1`（預設）= 把清單截成前 N 檔、只記 `symbol_list_exceeds_limit_truncated` warning，**超出的標的不會被訂閱**；設 `0` = 啟動或重載時直接拋 `ValueError`（`client.py:981–1000`） | 預設會靜默縮小 universe；要看 warning 或 `symbol_reload` 的 `exceeds_limit` 計數 |
| `HFT_STRICT_SUBSCRIPTION_LIMIT` | `0` | `1` = 超過上限時拒絕（拋 `ValueError`，log 為 `symbol_list_exceeds_limit`），不截斷（`client.py:977`） | 與上一列任一個啟用即為嚴格 |
| `HFT_ALLOW_SYMBOL_FALLBACK` | `0` | `1` = 以 `code` 找不到合約時，改用合約的 `symbol` 欄位比對，log 為 `Symbol fallback used for contract`（`client.py:264`、`contracts_runtime.py:692–700`） | — |
| `HFT_ALLOW_SYNTHETIC_CONTRACTS` | `0` | `1` = 訂閱時 SDK 找不到合約，改由 `_build_synthetic_contract` 建出合成合約（`client.py:265`、`subscription_manager.py:212`、`contracts_runtime.py:620–621`） | — |

### 7.1 Session Refresh / 重新登入（Shioaji）

適用於每個 quote facade 各自的 session-refresh 執行緒。**會登入 broker**，所以這些
旋鈕直接決定會不會踩到 `code: 451, Too Many Connections`。

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_SESSION_REFRESH_JITTER_FRAC` | `0.15` | 每個 facade 的檢查睡眠乘上 ±此比例，避免多個 facade 永遠同相醒來 | 設 `0` 會讓各 facade 回到鎖步，就是 2026-07-24 451 風暴的成因 |
| `HFT_SESSION_REFRESH_STAGGER_S` | `5.0` | 全行程登入槽（login slot）強制的連續登入最小間隔（秒） | 觀測到的實際登入延遲約 2 s；調到低於該值等於取消序列化保護 |
| `HFT_SESSION_REFRESH_STAGGER_TIMEOUT_S` | `120.0` | 取不到登入槽的等待上限（秒）。逾時後仍照常 refresh，並累加 `shioaji_login_slot_timeouts_total` | 刻意設計成 fail-open：過期的 session 比一次 451 更糟，而 451 重試路徑已能處理 |
| `HFT_SESSION_RELOGIN_POLL_S` | `60` | facade 處於登出狀態時，重新登入的輪詢間隔（秒） | 檢查間隔本身是寫死的 3600 s；此旋鈕存在的理由，是讓已登出的 facade 不必等滿一小時才嘗試第一次復原 |
| `HFT_SESSION_RELOGIN_BACKOFF_S` | `60` | 重新登入失敗後的起始退避（秒），每次失敗加倍，上限為 3600 s 的檢查間隔 | 每次嘗試都會經過同一個登入槽，因此縮短此值不會造成登入風暴 |

**Runbook 參考**: [feed-reconnect](../runbooks/feed-reconnect.md)

**Runbook 參考**: [Section 1 — Feed Gap](../runbooks.md#1-feed-gap--無行情), [Section 2 — Shioaji API latency](../runbooks.md#2-shioaji-api-latency-激增), [Section 14 — Quote Schema 不符](../runbooks.md#14-quote-schema-不符version-mismatch), [shioaji-contract-refresh-operations](../runbooks/shioaji-contract-refresh-operations.md)

---

## 8. Gateway & Feature Engine

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_GATEWAY_ENABLED` | `0` | `1` = 啟用 CE-M2 Order/Risk Gateway（多節點路由） | 單節點部署保持 `0` |
| `HFT_GATEWAY_HALT_CANCEL` | `1` | 預設 = HALT 時 `GatewayPolicy` 無條件放行 CANCEL。`0`／`false`／`no`／`off` 時，CANCEL 改走 `_reduces_position` 與 HALT_EXEMPT 判斷，兩者都不成立就以 `HALT` 拒絕；`FORCE_FLAT` 不受影響（`gateway/policy.py:56`、`:138–148`；compose `docker-compose.yml:82`） | 設 `0` 會讓 HALT 中的撤單可能被拒，與「HALT 阻擋新單、撤單仍允許」的不變式不一致，沒有明確理由不要設 |
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
| `HFT_EVENT_MODE` | `event` | `event` = normalizer 每個 tick 建立 `MetaData` 事件物件；`tuple` 或 `raw` = 回傳 tuple，跳過 dataclass 配置；**同時也跳過** `timebase.now_ns()` 本機時間戳、`seq`、`_validate_and_sync_timestamp` 與 `_record_latency_metrics`，tuple 只帶交易所時間戳（`:936–938`、`:982–995`、`:1092–1107`、`:1419–1436`、`:1501–1502`）。pytest 下強制為 `event`（`feed_adapter/normalizer.py:29–32`，tick 分支 `:982–1000`；compose `docker-compose.yml:34`） | 預設 `event` 在熱路徑上每 tick 配置一個物件；要改預設得先量測配置與延遲 |
| `HFT_BUS_RUST` | 見用途 | 同一檔案讀兩次，**未設時預設不同**：(1) `_USE_RUST_BUS` 預設為真，搭配 `HFT_RUST_ACCEL`（預設 `1`）且 Rust ring 可用時，事件匯流排使用 Rust ring（`engine/event_bus.py:20`、`:315`）；(2) `_BUS_MODE` 只在 `HFT_BUS_MODE` 未設且 `HFT_BUS_RUST == "1"` 才解析成 `rust_pyobj`，否則是 `python`（`:26–30`） | 要確認生效的匯流排，不能只看這個變數 |
| `HFT_BUS_MODE` | 空（由上列解析為 `python`） | 明確指定匯流排模式；只有 `rust_typed` 會改變實作（換成 `FastTypedRingBuffer`），載入失敗時退回 `python` 並記 warning（`engine/event_bus.py:28–60`、`:331`） | — |
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
| `HFT_MANUAL_LATCH_DIR` | `.state/manual_latches` | 需人工解除的 latch（`daily_loss`、`reconciliation_mismatch`）各存成一個 JSON 檔，另有 `audit.jsonl`；見 `docs/runbooks/halt-recovery.md` | 必須指向重啟後仍保留的路徑，否則 latch 會隨容器消失 |
| `HFT_PLATFORM_REDUCE_ONLY_FEED_GAP_S` | `600` | Active-symbol-aware feed-gap 觸發 `feed_reconnect_unhealthy` 的秒數門檻；2026-04 由 120s 提升為 600s，配合 800 標的訂閱宇宙與 illiquid 期權容忍度 | 視訂閱集流動性調整；過低易在 night session 誤觸發 |

重點：
- `platform_reduce_only_active=1` 時，shadow 策略即使仍在產生 intents，也可能沒有任何 new order 能往下游流動。
- Shadow mode (`HFT_ORDER_SHADOW_MODE=1`) 繞過 reduce-only gate — shadow orders 無金融風險，不受 reduce-only 限制。
- 自動恢復僅適用於 feed 相關觸發（`feed_reconnect_unhealthy`, `feed_gap_exceeded`）。若有非 feed 觸發（如 reconciliation drift），自動恢復會被阻擋。
- `manual_rearm_required=1` 表示依賴問題解除後仍需人工 re-arm（除非自動恢復已啟用且所有觸發條件已清除）。

### 8.2 自動 HALT 門檻

下列門檻決定佇列或匯流排的壓力事件累積到什麼程度時呼叫 `StormGuard.trigger_halt`。佇列滿與 raw 丟棄在未達門檻時先呼叫 `trigger_storm`；事件匯流排沒有 storm 這一步，達到門檻就直接 HALT：

| 變數 | 預設值 | 用途 | 位置 |
|---|---|---|---|
| `HFT_QUEUE_FULL_HALT_THRESHOLD` | `3` | 連續有 intent 因風控佇列滿被丟棄的事件數（逐事件計，不是逐 intent）；未達門檻 `trigger_storm("risk_queue_full")`，達到即 `trigger_halt("risk_queue_full_persistent")` | `strategy/runner.py:436`、`:1671–1679` |
| `HFT_ORDER_QUEUE_FULL_HALT_THRESHOLD` | `3` | 風控送往下單佇列連續滿的次數；達到即 `trigger_halt("order_queue_full_persistent")` | `risk/engine.py:245`、`:557–561` |
| `HFT_BUS_OVERFLOW_HALT_THRESHOLD` | `3` | 單一消費者連續的 overflow 次數（該消費者有一次沒有溢位的讀取就歸零，`:728–732`）；達到即 `trigger_halt` | `engine/event_bus.py:355`、`:684` |
| `HFT_BUS_OVERFLOW_RATE_THRESHOLD` | `10` | 整條匯流排（所有消費者合計）在 `HFT_BUS_OVERFLOW_WINDOW_S` 秒內的 overflow 次數；達到即 `trigger_halt("EventBus overflow rate: …")`，不看上一列的連續計數，所以間斷的 overflow 也會累積到 HALT | `engine/event_bus.py:360`、`:369–385`；compose `docker-compose.yml:98` |
| `HFT_BUS_OVERFLOW_WINDOW_S` | `60` | 上一列的滑動窗口（秒） | `engine/event_bus.py:359`；compose `docker-compose.yml:99` |
| `HFT_RAW_DROP_HALT_THRESHOLD` | `200` | raw 佇列連續丟棄的筆數；達到即 `trigger_halt("raw_queue_sustained_drops…")`；較低的 `HFT_RAW_DROP_DEGRADE_THRESHOLD`（預設 `50`）先 `trigger_storm`，並對 FeatureEngine `mark_gap_all()`；另有滑動窗口 `HFT_RAW_DROP_WINDOW_THRESHOLD`（預設 `60`）與 `HFT_RAW_DROP_WINDOW_S`（預設 `5.0`），只 `trigger_storm`，不會 HALT | `services/market_data.py:541–542`、`:547–548`、`:1907–1934` |

調低任何一個，StormGuard 會更早進入 HALT。HALT 阻擋新單；撤單、`FORCE_FLAT` 與減倉單仍允許（`gateway/policy.py:138–148`）。

---

## 9. 可觀測性

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_EXPOSURE_MAX_SYMBOLS` | `10000` | ExposureStore 最大標的數（記憶體基數上限） | 標的少時可縮小至 1000 |
| `HFT_RECORDER_MODE` | `direct` | `wal_first` = WAL-only 寫入路徑（CE-M3） | 生產環境建議 `wal_first` |
| `HFT_TS_TZ` | `Asia/Taipei` | 時區假設（無時區時間戳解析）。值無效時 `core/timebase.py` 記 warning 並退回 UTC（`core/timebase.py:43–49`），而 `core/market_calendar.py:60–70` 與 `core/session_hooks.py:119–125` 讀同一個變數，無效時退回的是固定 UTC+8 | 跨區部署務必顯式設定；值寫錯會讓兩邊對交易日的判斷不同 |
| `HFT_TS_ASSUME_TZ` | — | `HFT_TS_TZ` 的別名：只有 `core/timebase.py:44` 在 `HFT_TS_TZ` 未設時讀它，日曆與 session hooks 不讀 | 設時區請用 `HFT_TS_TZ` |
| `HFT_RECONNECT_TZ` | `Asia/Taipei` | reconnect 時段判斷時區 | 與 `HFT_TS_TZ` 保持一致 |
| `HFT_RECONNECT_DAYS` | `mon,tue,wed,thu,fri` | 自動重連啟用日（逗號分隔星期縮寫） | 週末 / 假日設空字串停用 |
| `HFT_RECONNECT_HOURS` | `08:30-13:35` | 交易時段自動重連窗口 | 開盤前/收盤後不觸發 |
| `HFT_RECONNECT_HOURS_2` | — | 第二交易時段窗口（期貨夜盤等） | 無需時可留空 |
| `HFT_RECONNECT_COOLDOWN` | `30` | 重連冷卻秒數（`feed_adapter/shioaji/reconnect_orchestrator.py:71`） | 避免頻繁重連 |
| `HFT_RECONNECT_BACKOFF_S` | `30` | 初始重連退避延遲（秒）（`feed_adapter/shioaji/client.py:394`、`_config.py:253`） | 指數退避起始值 |
| `HFT_RECONNECT_BACKOFF_MAX_S` | `120` | 最大重連退避延遲（秒） | 封頂避免過長等待 |
| `HFT_LOGIN_CONNLIMIT_RETRIES` | `2` | broker 連線數上限（451）拒絕登入時的退避重試次數；0 = 停用（立即 fail-closed） | 重啟競態（session 未釋放）的耐受度；見 2026-06-21/22 failed-attempts |
| `HFT_LOGIN_CONNLIMIT_BACKOFF_S` | `75` | 每次 451 退避重試前等待秒數（等 broker 釋放前一 session 的槽位） | 需大於 broker session 釋放窗口（約 60s） |
| `HFT_QUOTE_FLAP_THRESHOLD` | `5` | 報價閃爍偵測：窗口內最大閃爍次數 | 超過則暫停訂閱 |
| `HFT_QUOTE_FLAP_WINDOW_S` | `60` | 報價閃爍偵測窗口（秒） | 配合 threshold |
| `HFT_QUOTE_FLAP_COOLDOWN_S` | `300` | 報價閃爍冷卻（秒） | 冷卻後自動重新訂閱 |

> `HFT_QUOTE_FLAP_*` 系列變數共同控制報價閃爍偵測行為。
| `HFT_STORMGUARD_FEED_GAP_HALT_S` | `30` | 行情斷流觸發 HALT 門檻（秒） | 超過此值進入 HALT |
| `HFT_STORMGUARD_ORDER_RTT_STORM_COUNT` | `3` | 幾筆下單 RTT ≥ `order_rtt_storm_us` 才進 STORM（只能減倉） | 1 = 舊的單筆規則 |
| `HFT_STORMGUARD_ORDER_RTT_STORM_WINDOW_S` | `60` | 上述幾筆須落在幾秒內 | 配合 count |
| `HFT_STORMGUARD_ORDER_RTT_STORM_DWELL_S` | `300` | 最後一筆慢單後要安靜幾秒 STORM 才結束 | 越長擺盪越少、停報價越久 |
| `HFT_RECON_LATCH_RELEASE_AFTER_CLEAN` | `2` | 連續幾次乾淨對帳自動解除 `reconciliation_mismatch` latch | 至少 1 |
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
| `HFT_GIT_SHA` | `unknown` | 建置時由 `Dockerfile` 烘進 image 的 commit SHA；標示的是**image**，不是實際執行的原始碼（production 的 `src/` 是 bind mount）。用於 `hft_build_info` metric、config snapshot 與部署驗證 | 由建置流程設定，不要手動覆寫；要確認實際跑的程式碼請看 `code_identity` 的 `running_code_sha` |
| `HFT_HALT_AUTO_FLATTEN` | `0` | `1`／`true`／`yes`／`on` = 啟用 HALT 自動平倉；預設關閉（`risk/halt_flattener.py`） | 事故排查時確認未啟用；程式不讀 `HFT_AUTO_FLATTEN_DISABLED` |
| `HFT_LIVE_CONFIRM` | — | `HFT_ORDER_MODE=live` 時必須等於 `yes-i-know`（去除前後空白、不分大小寫）；其他任何值，包括 `1`，啟動時 `SystemExit(1)`（`services/bootstrap.py:244–250`） | 只有 `yes-i-know` 有效，見 [live-trading-activation-sop](../runbooks/live-trading-activation-sop.md) |
| `HFT_SESSION_GOVERNOR_ENABLED` | `0` | `1` = 啟用 session governor 自動管理 | 多帳號場景使用 |

> `HFT_ENGINE_IMAGE` 沒有任何讀取者（compose 讀的是 §4 的 `HFT_IMAGE`，預設 `hft-platform:latest`，
> `docker-compose.yml:13`），設了不會有效果。

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
| `HFT_CH_DATA_ROOT` | — | 只有 `ops.sh` 讀取：設定時 export `CH_DATA_HOT=$HFT_CH_DATA_ROOT/hot`、`CH_DATA_COLD=$HFT_CH_DATA_ROOT/cold`（`ops.sh:55–58`）。`/var/lib/clickhouse` 是容器內路徑，不是這個變數的預設值 | 例：`HFT_CH_DATA_ROOT=/mnt/data/clickhouse`（`docs/runbooks/low-latency-tuning.md:20`） |
| `HFT_CH_DATA_HOT`、`HFT_CH_DATA_COLD` | — | 只有 `ops.sh` 讀取，個別覆蓋 `CH_DATA_HOT`、`CH_DATA_COLD`，優先於 `HFT_CH_DATA_ROOT` 推得的值（`ops.sh:59–64`） | — |
| `CH_DATA_HOT`、`CH_DATA_COLD` | `ch_data_hot`、`ch_data_cold`（compose 具名 volume） | compose 掛載的 hot／cold 資料位置，未設定時用具名 volume（`docker-compose.yml:250–251`） | 要掛主機目錄時設絕對路徑 |

### 14.2 自動備份

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_BACKUP_ENABLED` | `0` | `1` = 啟用每日自動備份（`backup` service） | 生產環境設 `1` |
| `HFT_BACKUP_RETAIN_DAYS` | `7` | 每日備份保留天數 | 磁碟不足時縮短至 7-14 |
| `CH_BACKUP_PATH` | `./backups/clickhouse` | 備份 host volume 路徑 | 跨磁碟備份請指向獨立磁區 |

---

## 15. Telegram 通知

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_TELEGRAM_ENABLED` | `0` | `1` = 啟用 Telegram bot 推播（事故 / 平倉 / HALT） | 生產環境設 `1` |
| `HFT_TELEGRAM_BOT_TOKEN` | — | BotFather 取得的 token | 使用 secret manager；勿硬編 |
| `HFT_TELEGRAM_CHAT_ID` | — | 接收訊息的 chat id（可由 `getUpdates` 查得） | 群組 chat id 為負數 |
| `HFT_REPORT_SYMBOLS` | `TXFR1` | bot 日報與 `/report` 的商品清單（逗號分隔；`bot/app.py:93`） | compose 的 `hft-bot` 以相同預設傳入 |
| `HFT_REPORT_ENABLED` | `0` | 只管 CLI（`python -m hft_platform.reports`）：非 `1` 時，沒有 `--dry-run`／`--debug` 就不執行（`pipeline.py:300`） | bot 的 `/report` 與排程推播不讀它 |
| `HFT_LLM_ENABLED` | `0` | `1` = 把日報 dossier 送到 OpenRouter（`pipeline.py:178`）；其他值不送任何資料 | 外送內容清單見 `docs/modules/reports.md` 的「LLM egress」 |
| `HFT_LLM_API_KEY` | — | OpenRouter 金鑰（`llm_client.py:52`）；缺少時每次 LLM 呼叫都 raise，報告退回規則版 | 敏感；使用 secret manager |
| `HFT_LLM_MODEL` | 空字串 | OpenRouter 模型 id（`pipeline.py:183`、`llm_reasoner.py:210`） | — |

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

## 18. Research 證據根（ADR-005）

兩個根目錄都必須是絕對路徑、位於 repo 之外；呼叫時才讀取。實作：`src/hft_platform/alpha/evidence.py`。

| 變數 | 預設值 | 用途 | 調整建議 |
|---|---|---|---|
| `HFT_RESEARCH_EVIDENCE_ROOT` | 無（governed run 必填）；exploratory 讀取時為 `~/hft_research_evidence` | strict（governed）research run 的輸出根：`runs/<run_id>/`、`validations/`，每個目錄以 `manifest.json` 封存，判定事件追加到 `ledger/verdicts.jsonl` | 指向私有 git repo；未設定、相對路徑或位於 repo 內時 strict run 以 rc 2 停止 |
| `HFT_RESEARCH_DATA_ROOT` | `~/hft_research_data` | research 資料集根目錄（不進 git） | 同上，必須位於 repo 之外 |
| `HFT_RESEARCH_ALLOW_TRIAGE` | `0` | 設為 `1` 才允許 `research.pipeline triage` / `hft alpha pipeline triage`（略過 gate 的除錯模式，輸出永遠不可 promote）；實作：`research/pipeline.py`、`research/registry/alpha_cli.py` | 只在本機除錯時臨時設定，不寫入任何 env 檔 |

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
