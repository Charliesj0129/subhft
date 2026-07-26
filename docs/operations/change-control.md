# Ops Change Control

定義基礎變更管控流程，避免線上風險。

> 本文件管的是**變更的紀錄與證據**（變更單、drift、release channel、稽核產物）。
> 實際**怎麼把變更送上生產主機**，見 `docs/runbooks/deployment.md`（Deploy Laws
> D1–D10）。兩者互補：先過本文件的變更單與 gate，再依 runbook 的部署分級執行。

## Scope
- `docker-compose.yml` / `docker-compose.production.yml`
- `config/` 下會影響 live 行為的變更
- `ops.sh` 與 `scripts/` 中運維腳本
- 監控告警規則變更（Prometheus/Grafana/Alertmanager）

## Workflow
1. 建立變更單：`what / why / risk / rollback`。
2. 至少一位 reviewer 確認。
3. 先在 `sim` 或 staging 驗證。
4. 判定**部署分級**（runbook D5）：A 程式碼／B 監控規則／C env·compose·image。
   分級決定機制、是否重啟引擎、以及可寫層風險。
5. 事前寫下具名 pass criteria 並取得重啟前 baseline（runbook D9）。
   驗證面向：feed、queue、risk reject、recorder、/metrics scrape。
6. 若異常，5 分鐘內執行 rollback（backup 依 runbook D4 事前備妥）。
7. 部署後補上 `~/deploy_ledger.tsv` 一行（runbook D10）——
   下一次變更的 drift 比對以此為基準。

## WS-D 自動化（2026-03-05）

部署前建議固定執行：

```bash
# 1) 建 baseline（第一次或每次核准版本）
make deploy-drift-snapshot

# 2) 生成 pre-sync 產物（backup + rollback template + snapshot）
make deploy-pre-sync-template CHANGE_ID=CHG-YYYYMMDD-XX

# 3) 比對 drift（部署前/部署後都可執行）
make deploy-drift-check BASELINE=outputs/deploy_guard/snapshots/<baseline>.json
```

`deploy-pre-sync-template` 會產出：
- `pre_sync_snapshot.json`
- `backup_<change_id>.tar.gz`
- `rollback.sh`
- `change_template.md`
- `manifest.json`

## WS-D Release Channel（2026-03-05）

部署要從 canary 提升到 stable 前，執行：

```bash
# 1) gate: 檢查 pre-sync manifest + canary + drift 證據
make release-channel-gate CHANGE_ID=CHG-YYYYMMDD-XX

# 2) promote: gate pass 後寫入 stable promotion 稽核紀錄
make release-channel-promote CHANGE_ID=CHG-YYYYMMDD-XX ACTOR=ops
```

`release-channel-gate` 判定條件：
- `pre_sync manifest` 存在且包含 backup/rollback/template 產物。
- 最新 canary 報告整體為 `pass`（預設策略）。
- 最新 drift check 整體為 `pass`（預設策略）。

若要把「代碼 gate + 發布證據 + 月度運營證據」合併成單一 go/no-go 入口，使用：

```bash
HFT_ALPHA_AUDIT_ENABLED=1 make release-first-ops-gate CHANGE_ID=CHG-YYYYMMDD-XX
```

此指令會聚合：
- `release_converge --skip-clean --skip-gate`
- strict `roadmap_delivery_executor` / `roadmap_delivery_guard`
- release 關鍵 unit tests + `ruff` + `mypy`
- `release_channel_guard gate`
- `reliability_review_pack`

輸出：
- `outputs/release_first_ops/release_first_ops_*.json`
- `outputs/release_first_ops/release_first_ops_*.md`
- `outputs/release_first_ops/latest.json`
- `outputs/release_first_ops/latest.md`

只有 `release-first-ops-gate` 通過後，才應執行：

```bash
HFT_ALPHA_AUDIT_ENABLED=1 make release-first-ops-promote CHANGE_ID=CHG-YYYYMMDD-XX ACTOR=ops
```

輸出證據：
- `outputs/deploy_guard/release_channel/decisions/release_gate_*.json`
- `outputs/deploy_guard/release_channel/decisions/release_gate_*.md`
- `outputs/deploy_guard/release_channel/promotions/stable_*.json`（僅 `promote --apply` 且 gate pass）

## WS-D 月度可靠性審查包（2026-03-05）

每月例行審查前可執行：

```bash
# 產生月度包（含 soak/backlog/drift/disk/drill/query-guard/feature-canary/callback-latency）
make reliability-monthly-pack MONTH=YYYY-MM RUN_DRILL=1 QUERY_GUARD_MIN_RUNS=1 QUERY_GUARD_MIN_SUITE_RUNS=1 FEATURE_CANARY_MIN_RUNS=1 CALLBACK_LATENCY_MIN_RUNS=1
```

輸出：
- `outputs/reliability/monthly/monthly_<YYYY-MM>_*.json`
- `outputs/reliability/monthly/monthly_<YYYY-MM>_*.md`
- `outputs/reliability/monthly/drill_checks/drill_*.json`（`RUN_DRILL=1` 時）

## WS-B 線上查詢護欄（2026-03-05）

針對 ClickHouse 線上診斷查詢，避免運維查詢打爆記憶體：

```bash
# 先做 guard check（read-only + full-scan 防護）
make ch-query-guard-check QUERY='SELECT ... LIMIT 1000'

# 再執行受限查詢（readonly + memory/time/result guard）
make ch-query-guard-run QUERY='SELECT ... LIMIT 1000'

# 批次基線查詢（建議每日 cron，產生可稽核 suite 證據）
make ch-query-guard-suite
```

`ch-query-guard-suite` 會使用 `config/monitoring/query_guard_suite_baseline.json` 批次執行，
並輸出：
- `outputs/query_guard/suites/suite_*.json`
- `outputs/query_guard/suites/suite_*.md`

## WS-B DLQ 回補作業（2026-03-05）

若發生 WAL 批次落入 DLQ（`insert_failed_after_retries`），使用標準流程：

```bash
# 1) 先看規模
make wal-dlq-status

# 2) dry-run（不搬檔）
make wal-dlq-replay-dry-run MAX_FILES=50

# 3) 小批正式回補
make wal-dlq-replay MAX_FILES=50

# 4) 清理 WAL manifest 孤兒暫存檔（可選）
make wal-manifest-tmp-cleanup MIN_AGE_SECONDS=300
```

輸出證據：
- `outputs/wal_dlq/status/*.json|*.md`
- `outputs/wal_dlq/replay/*.json|*.md`
- `outputs/wal_dlq/cleanup_tmp/*.json|*.md`

## 最小驗證證據

以下是**下限**，不是完整驗證。生產部署另須跑完 runbook D9 的
verification floor（451 計數、error/critical log、每個 facade 的訂閱數與
thread liveness），並帶上該次變更專屬的指標。

- `docker compose ps`
- `docker compose logs --tail=200 hft-engine`
- `curl -fsS http://localhost:9090/metrics | head`
- `uv run hft recorder status`

`docker compose ps` 顯示 healthy **不構成**部署已驗證。

## Rollback
- 保留上一版 image/tag。
- 保留前一版 `.env`/config 備份（生產主機依 runbook D4 存於
  `~/deploy_backup_<UTC-date>/<batch>/`，含 `MD5SUMS.txt`）。
- 回滾後重新執行與部署時**同一組** pass criteria——未驗證的 rollback
  等於第二次未測試的變更。
- 演練：`make rollback-drill`（`scripts/rollback_drill.py`）。
