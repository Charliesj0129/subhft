# Old-PC One-Year Reliability Appendix

目的：將「部署營運常見致命模式」固定成可驗證的巡檢與處置標準，目標是舊電腦連續運行 1 年。

## A. 風險分類（必查）

1. `Crash`: 服務 task 意外退出，進入 ghost mode（程序活著但核心功能停擺）。
2. `No IO`: 行情/執行/落盤吞吐降為 0 或接近 0。
3. `Reconnect Storm`: 短時間內反覆重連，造成行情抖動與 CPU/網路壓力。
4. `Disk Growth`: WAL / archive 無上限增長，最終導致寫入失敗或系統不穩。
5. `Session Conflict`: 多 runtime 競爭同一 broker session，導致 feed 異常。

## B. 指標基線（可驗證）

用以下命令抓本機指標：

```bash
curl -fsS http://localhost:9090/metrics > /tmp/hft_metrics.txt
```

關鍵指標與期望：

1. `execution_gateway_alive == 1`、`execution_router_alive == 1`（持續）。
2. `rate(feed_events_total[30s]) > 0`（交易時段）。
3. `increase(feed_reconnect_timeout_total[5m]) == 0`（理想）。
4. `increase(raw_queue_dropped_total[5m]) == 0`（理想）。
5. `wal_backlog_files` 不持續單向上升；`wal_drain_eta_seconds` 可回落。
6. `increase(feed_session_conflict_total[10m]) == 0`、`increase(shioaji_session_lock_conflicts_total[10m]) == 0`。
7. `increase(shioaji_login_fail_total[10m]) == 0`。

## C. 巡檢節奏

1. 每日（交易前）：檢查 B 節 1,2,6,7。
2. 每日（收盤後）：檢查 B 節 3,4,5。
3. 每週：執行一次 WAL/Replay 回歸與災難演練套件（見 `docs/runbooks/wal-first-outage-drills.md`）。
4. 每月：檢查 `.wal/archive` 成長趨勢與清理策略是否生效。
5. 每月：產生可靠性審查包（soak/backlog/drift/disk/drill/query-guard/feature-canary/callback-latency）並留存稽核。

自動化建議（已提供腳本）：

1. 每日產生報告：
```bash
python3 scripts/soak_acceptance.py daily \
  --project-root /home/charl/subhft \
  --prom-url http://localhost:9091 \
  --output-dir /home/charl/subhft/outputs/soak_reports \
  --allow-warn-exit-zero
```
2. 每週彙總報告：
```bash
python3 scripts/soak_acceptance.py weekly \
  --project-root /home/charl/subhft \
  --prom-url http://localhost:9091 \
  --output-dir /home/charl/subhft/outputs/soak_reports
```
3. 每週 canary 簽核：
```bash
python3 scripts/soak_acceptance.py canary \
  --project-root /home/charl/subhft \
  --prom-url http://localhost:9091 \
  --output-dir /home/charl/subhft/outputs/soak_reports \
  --window-days 10 \
  --min-trading-days 5 \
  --min-first-quote-pass-ratio 1.0 \
  --max-reconnect-failure-ratio 0.2 \
  --max-watchdog-callback-reregister 120 \
  --allow-warn-exit-zero
```
4. 每週 callback latency guard：
```bash
python3 scripts/callback_latency_guard.py \
  --prom-url http://localhost:9091 \
  --window 30m \
  --output-dir /home/charl/subhft/outputs/callback_latency \
  --allow-warn-exit-zero
```
5. 每日 query-guard 基線批次：
```bash
python3 scripts/ch_query_guard_suite.py \
  --profile /home/charl/subhft/config/monitoring/query_guard_suite_baseline.json \
  --output-dir /home/charl/subhft/outputs/query_guard \
  --container clickhouse \
  --host localhost \
  --port 9000 \
  --user default
```
6. 每日 DLQ 風險快照（若 files > 0，需啟動回補程序）：
```bash
python3 scripts/wal_dlq_ops.py status \
  --wal-dir /home/charl/subhft/.wal \
  --archive-dir /home/charl/subhft/.wal/archive \
  --output-dir /home/charl/subhft/outputs/wal_dlq \
  --allow-warn-exit-zero
```
7. 每月審查包：
```bash
python3 scripts/reliability_review_pack.py \
  --project-root /home/charl/subhft \
  --soak-dir /home/charl/subhft/outputs/soak_reports \
  --deploy-dir /home/charl/subhft/outputs/deploy_guard \
  --query-guard-dir /home/charl/subhft/outputs/query_guard \
  --feature-canary-dir /home/charl/subhft/outputs/feature_canary \
  --callback-latency-dir /home/charl/subhft/outputs/callback_latency \
  --output-dir /home/charl/subhft/outputs/reliability/monthly \
  --month YYYY-MM \
  --disk-path /home/charl/subhft \
  --disk-path /home/charl/subhft/.wal \
  --min-query-guard-runs 1 \
  --min-query-guard-suite-runs 1 \
  --min-feature-canary-runs 1 \
  --min-callback-latency-runs 1 \
  --run-drill-suite \
  --allow-warn-exit-zero
```

從開發機遠端執行（不進遠端 shell）：

```bash
python3 scripts/soak_acceptance.py daily \
  --ssh-target charl@100.91.176.126 \
  --project-root /home/charl/subhft \
  --prom-url http://localhost:9091 \
  --output-dir outputs/soak_reports \
  --allow-warn-exit-zero
```

## D. 本版已落地的防護

1. `HFTSystem` 監督器覆蓋擴大為全關鍵 task，並加入 restart backoff 防重啟風暴。
2. `HFTSystem` 關機流程加入 bootstrap teardown，避免 Redis session lease 遺留。
3. Redis lease 釋放改為「只刪自己的 owner key」，避免誤刪其他 runtime 的 lease。
4. WAL loader 新增 archive retention cleanup，避免 archive 無界成長。
5. 補齊告警：
   - `ExecutionGatewayTaskDown`
   - `ExecutionRouterTaskDown`
   - `RawQueueDropsDetected`
   - `FeedSessionConflictDetected`
   - `ShioajiSessionLockConflictDetected`
   - `ShioajiLoginFailuresDetected`

## E. 事件處置最短路徑

1. 若 `execution_*_alive == 0`：先看 `hft-engine` logs 是否連續 crash/restart。
2. 若 `feed_events_total` 無增量：先看 `ShioajiLoginFailuresDetected`、`FeedSessionConflictDetected`。
3. 若 `raw_queue_dropped_total` 增加：檢查 callback 入站壓力與 queue 容量。
4. 若 `wal_backlog_files` 持續上升：確認 ClickHouse 可用、loader 正常、磁碟可寫。
5. 若重連暴增：檢查交易時段 gating 是否生效、網路/券商端異常、session owner 衝突。

## F. 仍需追蹤（非阻塞）

1. `shioaji_client.py` 已達 <1500 行，後續聚焦 legacy shim 低風險清理與介面穩定性驗證。
2. 生產 canary 指標驗收（first quote callback / reconnect 成功率 / watchdog callback re-register）已納入每週 `soak_acceptance.py canary`，持續觀察 burn-in 趨勢。
