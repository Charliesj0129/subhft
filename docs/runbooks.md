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

診斷指標（正常值 vs. 告警值）：
- `feed_events_total` 應在交易時段持續遞增（>50 events/min）。
- `feed_gap_by_symbol_seconds[symbol]` 持續 >10s 視為異常。
- `shioaji_thread_alive[quote_watchdog]` = 1 表示 watchdog 正常運作。

升級路徑（重啟後仍無行情）：
1. 確認 watchdog 啟動：`docker compose logs hft-engine | rg "quote_watchdog"`。
2. 調整 quote 閾值（縮短）: `HFT_QUOTE_NO_DATA_S`（預設 10s）、`HFT_QUOTE_WATCHDOG_S`（預設 5s）。
3. 查看 `shioaji_quote_pending_stall_total` 是否持續累積（表示 watchdog 在持續 stall 中但無法恢復）。
4. 若 `quote_version_switch_total[direction=downgrade]` 出現但未恢復 → 手動固定 `HFT_QUOTE_VERSION=v0`。
5. 仍無法恢復 → 參考 `docs/runbooks/shioaji-contract-refresh-operations.md`。

## 2) Shioaji API latency 激增

檢查：
```bash
curl -fsS http://localhost:9090/metrics | rg "shioaji_api_latency_ms"
uv run python scripts/latency/shioaji_api_probe.py --mode sim --iters 30
```

處置：
- 檢查網路抖動與封包遺失。
- 視需要調整 `HFT_API_MAX_INFLIGHT`、`HFT_API_QUEUE_MAX`。

正常值基準（依 docs/architecture/latency-baseline-shioaji-sim-vs-system.md）：
- `shioaji_api_latency_ms[op=place_order]` P95 ~ 30–50ms (sim)；超過 200ms 為告警。
- `shioaji_api_jitter_ms` > 20ms 持續時代表網路不穩。

升級路徑：
1. 若延遲 >500ms 且持續 → 查看 `shioaji_keepalive_failures_total`（keepalive 失效）。
2. 調低 `HFT_API_MAX_INFLIGHT`（減少並發）；調低 `HFT_API_QUEUE_MAX`。
3. 若為 VPN/防火牆因素 → 切換備援網路後 `docker compose restart hft-engine`。

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
4. 線上查詢優先使用 guard wrapper（避免運維查詢造成 OOM）：
```bash
make ch-query-guard-check QUERY='SELECT count() FROM hft.market_data WHERE ingest_ts > now64() - 300000000000 LIMIT 1000'
make ch-query-guard-run QUERY='SELECT count() FROM hft.market_data WHERE ingest_ts > now64() - 300000000000 LIMIT 1000'
# 每日基線批次（建議以 cron 排程）
make ch-query-guard-suite
```

延伸 Runbook：
- [docs/runbooks/ch-mv-pressure-tuning.md](runbooks/ch-mv-pressure-tuning.md) — tuning parameters and INC-CHMV-20260303-01 incident record (Code 241 retries: 27 → 0–1 per 5m).
- 附錄：`Appendix A: Incident Record (2026-03-03)`

可調旋鈕（快速緩解）：
- `HFT_INSERT_MAX_RETRIES`（預設 3）：降低重試次數以減少 CH 壓力。
- `HFT_INSERT_BASE_DELAY_S`（預設 0.5s）：拉長重試間隔（e.g., 2.0）。
- `HFT_INSERT_MAX_BACKOFF_S`（預設 5s）：避免太快重試（e.g., 30s）。
- 再升級 → `docs/runbooks/ch-mv-pressure-tuning.md`。

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

診斷指標（閾值）：
- `wal_backlog_files` ≤ 20 正常；>50 SLO 告警；>200 緊急。
- `wal_replay_lag_seconds` ≤ 300s 正常；>600s SLO 告警。
- `disk_pressure_level` = 0 正常；= 1 警告；= 2 緊急；= 3 拒寫（HALT）。
- `wal_oldest_file_age_seconds` > 3600s → WAL loader 可能停滯。

升級路徑（WAL 堆積仍上升）：
1. 確認 loader 運作中：`docker compose ps wal-loader`。
2. 若 CH 仍有 Code 241 → 先處理 CH 記憶體壓力（Section 4）。
3. 調整 `HFT_WAL_POLL_INTERVAL_S`（預設 1.0s）縮短輪詢加速追回。
4. 若磁碟不足 → 執行 `make wal-archive-cleanup`（清理舊 archive）。
   可調 `HFT_ARCHIVE_RETENTION_DAYS`（預設 14 天）縮短以釋空間。
5. `disk_pressure_level=3` 時 WAL 完全停寫 → 參見「WAL 磁碟滿」Section 13。

## 6) Queue Depth 爆增 / Event Loop Lag

檢查：
```bash
curl -fsS http://localhost:9090/metrics | rg "queue_depth|event_loop_lag_ms"
```

處置：
- 調整 queue 容量（`HFT_*_QUEUE_SIZE`）。
- 檢查策略是否有阻塞 I/O。

各 Queue 容量（預設值）：
| Queue | 環境變數 | 預設容量 |
|---|---|---|
| raw (market data) | `HFT_RAW_QUEUE_SIZE` | 65,536 |
| raw_exec | `HFT_RAW_EXEC_QUEUE_SIZE` | 8,192 |
| risk | `HFT_RISK_QUEUE_SIZE` | 4,096 |
| order | `HFT_ORDER_QUEUE_SIZE` | 2,048 |
| recorder | `HFT_RECORDER_QUEUE_SIZE` | 16,384 |

告警：`queue_depth[raw_queue]` 持續 >90% 容量表示消費者落後。
調整容量時最小為 1024；重啟後生效。

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

## 9) Service task crash (supervisor restart)

徵兆：
- `execution_gateway_alive == 0`、`execution_router_alive == 0`，或類似 task-down 告警。
- Log 出現 `"Critical service task stopped"` 事件。

處置：
1. HFTSystem supervisor 會自動以指數退避重啟（1s → 30s max）。無需立即人工介入。
2. 若重啟迴圈持續，StormGuard 可能切換至 HALT 狀態 — 檢查 `storm_guard_state` metric。
3. 解決根本原因後手動執行 `hft run` 重新啟動引擎。

```bash
curl -fsS http://localhost:9090/metrics | rg "execution_gateway_alive|execution_router_alive|storm_guard_state"
docker compose logs --tail=300 hft-engine | rg "Critical service task stopped|supervisor|backoff"
```

可調旋鈕：
- `HFT_TASK_RESTART_BACKOFF_S`（預設 1.0s）：初始重啟等待。
- `HFT_TASK_RESTART_BACKOFF_MAX_S`（預設 30s）：重啟等待上限。
- `HFT_SUPERVISOR_QUEUE_LOG_EVERY_S`（預設 30s）：queue 深度日誌頻率。

判斷重啟風暴 vs. 正常恢復：
- 若任務在 <5s 重啟又再次崩潰超過 3 次 → 非暫時性錯誤，需人工調查日誌根因。
- 正常恢復：1–2 次重啟後 `execution_router_alive / execution_gateway_alive` 恢復 1。

## 10) 舊電腦一年期穩定運行附錄

附錄文件：
- `docs/runbooks/old-pc-yearly-reliability.md`

內容包含：
1. 當機 / 無 I/O / 重連風暴 / WAL 長期膨脹 的固定檢查基線。
2. 可驗證指標與日/週/月巡檢節奏。
3. 本版已落地防護與剩餘非阻塞追蹤項。

## 11) 每日 Soak 報告

每日 soak 報告（自動產出至 `outputs/soak_reports/`）：
```bash
make soak-daily-report
```

每週彙整報告：
```bash
make soak-weekly-report
```

## 12) Redis Session Lease 衝突

徵兆：
- 啟動日誌出現 `"feed_session_conflict: another runtime already holds the broker session"`。
- `feed_session_conflict_total[role]` 大於 0。

原因：前一個 engine 未正常關閉，Redis key 尚未過期（TTL 預設 300s）。

處置：
1. 等待 TTL 過期（最多 `HFT_FEED_SESSION_OWNER_TTL_S` 秒，預設 300s）。
2. 強制清除：
```bash
docker exec redis redis-cli DEL feed:session:owner
```
（Key 名稱由 `HFT_FEED_SESSION_OWNER_KEY` 控制，預設 `feed:session:owner`）
3. 確認清除後重啟：
```bash
docker compose restart hft-engine
```
4. 診斷多 runtime 場景 → `HFT_RUNTIME_INSTANCE_ID` 應於不同主機設為不同值。

指標監控：
- `feed_session_lease_ops_total[op=preflight,result=conflict]` — 衝突次數（應為 0）。
- `feed_session_lease_ops_total[op=refresh,result=ok]` — 正常 lease 續期（應持續增加）。

環境變數參考：`docs/operations/env-vars-reference.md`（Section 4: Redis Session 管理）。

## 13) WAL 磁碟滿（disk_pressure_level = 3）

徵兆：
- `disk_pressure_level = 3`（= HALT，拒寫）。
- WAL writer 拒絕寫入；新 tick 資料遺失風險。

處置（限時）：
1. 立即確認空間：
```bash
df -h $(docker inspect wal-loader --format '{{range .HostConfig.Binds}}{{.}} {{end}}' 2>/dev/null | tr ' ' '\n' | head -5)
df -h .wal
```
2. 快速釋空：`make wal-archive-cleanup`（清理 archive 舊檔）。
3. 調整 `HFT_ARCHIVE_RETENTION_DAYS`（縮短至 7）並重啟 wal-loader：
```bash
HFT_ARCHIVE_RETENTION_DAYS=7 docker compose restart wal-loader
```
4. 調整警告閾值以提早告警：
   - `HFT_WAL_SIZE_WARNING_MB`（預設 100 MB）
   - `HFT_WAL_SIZE_CRITICAL_MB`（預設 500 MB）
5. 若 DLQ/corrupt 目錄也過大：
   - `HFT_DLQ_RETENTION_DAYS`（預設 7）
   - `HFT_CORRUPT_RETENTION_DAYS`（預設 30）
6. 永久方案 → `docs/operations/long-term-risk-register.md` R1（磁碟耗盡風險）。

磁碟壓力等級說明：
| 等級 | 值 | 行為 |
|---|---|---|
| 正常 | 0 | WAL 正常寫入 |
| 警告 | 1 | 記錄告警日誌，繼續寫入 |
| 緊急 | 2 | 提高告警頻率，嘗試清理 |
| HALT | 3 | 拒絕新 WAL 寫入，防止磁碟爆滿 |

## 14) WAL DLQ 回補（`insert_failed_after_retries`）

徵兆：
- `.wal/dlq/` 出現大量 `*.jsonl`（代表 WAL 批次最終未成功寫入 CH）。
- `recorder_insert_batches_total{result=~"failed_after_retry|failed_no_client"}` 增加。

快速處置：
```bash
# 1) 看 DLQ 規模（輸出 outputs/wal_dlq/status/）
make wal-dlq-status

# 2) 先 dry-run 驗證（不搬檔、不寫入）
make wal-dlq-replay-dry-run MAX_FILES=50

# 3) 正式回補（可先小批）
make wal-dlq-replay MAX_FILES=50

# 4) 若有殘留 manifest 暫存檔，清理孤兒 .tmp
make wal-manifest-tmp-cleanup MIN_AGE_SECONDS=300
```

注意：
- `MAX_FILES` 建議先小批次（例如 50）驗證後再全量。
- `wal-dlq-replay` 成功後檔案會移至 archive；失敗檔保留於 DLQ。

## 15) Quote Schema 不符（Version Mismatch）

徵兆：
- `quote_schema_mismatch_total[expected, reason]` 持續增加。
- 行情回調資料缺欄位（bid_price/ask_price/close）。
- `shioaji_quote_pending_age_seconds` 持續上升（feed 停滯）。

原因：Shioaji API 版本 v1 payload 格式改變，與 schema guard 期望不符。

處置：
1. 確認 watchdog 自動降版：
```bash
docker compose logs hft-engine | rg "version_downgrade|schema_mismatch|Falling back to quote v0"
```
2. 若 watchdog 未自動降版（`quote_version_switch_total` 無記錄）：
```bash
# 手動固定至 v0
docker compose stop hft-engine
HFT_QUOTE_VERSION=v0 docker compose up -d hft-engine
```
3. 確認 `feed_events_total` 恢復增長。
4. 評估是否需升級 Shioaji SDK（若為 broker 端 API 更新）。

相關環境變數：
- `HFT_QUOTE_VERSION`（`auto`/`v0`/`v1`，預設 `auto`）：強制鎖定 quote 版本。
- `HFT_QUOTE_VERSION_STRICT`（`0`/`1`，預設 `0`）：`1` = 禁止自動降版。
