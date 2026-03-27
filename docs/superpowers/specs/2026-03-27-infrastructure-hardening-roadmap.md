# Infrastructure Hardening Roadmap

**Date:** 2026-03-27
**Scope:** Ops / Infra / Analytics — 上線前到穩定運營的基礎建設補完
**Approach:** 3 波次平行推進（Phased Parallel）

## Context

基於全面盤點，平台核心交易路徑已完成，但 ops/infra/analytics 層存在 8 項缺口。
本 roadmap 聚焦補完這些缺口，不含 core engineering 專項（Rust kernel promotion、ShioajiClient 拆分）。

## Excluded (另開專項或日常改善)

| Item | Reason |
|------|--------|
| Grafana | Monitor TUI 已取代 |
| CD pipeline 接通 | 維持手動部署 |
| Rust kernel promotion (TODO §1.4) | Core engineering 專項 |
| ShioajiClient 拆分 (TODO §1.2/CE2-D2) | Core engineering 專項 |
| Runbook SOP 完善 | 日常持續改善 |

---

## Wave 1 — 上線前必備

目標：系統出事時有人知道，dependency 不會無預警 break。

### 1-A. Alertmanager webhook → Telegram dispatcher

**Problem:** Alertmanager receiver 是 `http://localhost:9099/noop`，所有 alert 靜默丟棄。

**Solution:**
- 新增獨立 `AlertmanagerBridge` 模組，內建輕量 raw-asyncio HTTP server（與現有 `HealthServer` 同模式，不引入 aiohttp）
- 監聽獨立 port（`HFT_ALERT_BRIDGE_PORT`，default `8081`），接收 Alertmanager 標準 webhook payload（`alerts[]` with `status`, `labels`, `annotations`）
- 轉換成 Telegram 訊息格式
- 內建獨立 `TelegramSender(enabled=True)`，不依賴 bootstrap 中 disabled-by-default 的 sender。從 env vars `HFT_TELEGRAM_BOT_TOKEN` + `HFT_TELEGRAM_CHAT_ID` 讀取（已有 `validate_env.sh` 檢查）
- 更新 `config/monitoring/alerts/alertmanager.yml`：
  - receiver: `http://hft-engine:8081/webhook/alertmanager`
  - `group_wait: 30s`, `group_interval: 5m`, `repeat_interval: 4h`

**Design notes:**
- 不修改現有 `HealthServer`（port 9090），避免耦合 health + alerting
- 不依賴 `bootstrap.py` 中 `TelegramSender()` 的 default `enabled=False` 實例
- Bridge 啟動失敗不阻擋交易引擎

**Files touched:**
- New: `src/hft_platform/notifications/alertmanager_bridge.py`（raw-asyncio server + payload parser + TelegramSender(enabled=True)）
- Edit: `config/monitoring/alerts/alertmanager.yml`（noop → webhook URL）
- Edit: `src/hft_platform/services/bootstrap.py`（啟動 AlertmanagerBridge task）
- New: `tests/unit/test_alertmanager_bridge.py`

**Verification:** 手動觸發 test alert → Telegram 收到訊息。

### 1-B. Shioaji SDK pin 版本

**Problem:** `pyproject.toml` 寫 `shioaji[speed]>=1.2,<2`，任何 1.x 都可能被拉進來。

**Solution:**
- `uv.lock` 顯示當前鎖定版本為 `1.2.9`
- Pin 成 `shioaji[speed]==1.2.9`
- 加註解 `# PINNED: bump manually after SDK regression test`

**Files touched:**
- Edit: `pyproject.toml`
- Edit: `docs/operations/long-term-risk-register.md` (R10 → ✅ Done)

**Verification:** `uv lock --check` 通過。

### 1-C. Startup config snapshot → ClickHouse

**Problem:** 無法事後重建「出事時跑的是什麼 config」。

**Security: env var redaction（必須）**

`HFT_*` env vars 包含機密：`HFT_REDIS_PASSWORD`, `HFT_CLICKHOUSE_PASSWORD`, `HFT_FUBON_PASSWORD`, `HFT_MONITOR_REDIS_PASSWORD`, `HFT_TELEGRAM_BOT_TOKEN`。直接寫入 ClickHouse 等於把 credentials 複製到資料庫。

採用 **allowlist** 策略（不是 denylist — 新增的機密 env var 不會意外洩漏）：
- 定義 `CONFIG_SNAPSHOT_ALLOWED_PREFIXES`：只記錄非機密的 `HFT_*` vars
- Allowlist: `HFT_MODE`, `HFT_ORDER_MODE`, `HFT_SYMBOLS`, `HFT_BROKER`, `HFT_QUOTE_VERSION`, `HFT_STRICT_PRICE_MODE`, `HFT_GATEWAY_ENABLED`, `HFT_RECORDER_MODE`, `HFT_FEATURE_ENGINE_*`, `HFT_FUSED_*`, `HFT_EXPOSURE_*`, `HFT_STORMGUARD_*`, `HFT_RECONNECT_*`, `HFT_QUOTE_FLAP_*`, `HFT_BACKUP_*`, `HFT_STARTUP_RECON_*`, `HFT_CHECKPOINT_*`, `HFT_MONITOR_SOURCE`, `HFT_MONITOR_LIVE_ENABLED`, `HFT_OBS_POLICY`, `HFT_WAL_*`
- 任何含 `PASSWORD`, `SECRET`, `TOKEN`, `KEY`, `CERT` 的 var 名稱一律排除（defense-in-depth）
- 測試必須驗證：已知機密 var 不出現在 snapshot output 中

**Solution:**
- Bootstrap 階段收集：allowlisted `HFT_*` env vars + YAML config SHA256 + git commit hash
- 寫入 ClickHouse `hft.config_snapshots` 表
- Schema: `boot_ts DateTime64(3)`, `config_hash String`, `git_sha String`, `env_json String`, `yaml_json String`
- ClickHouse 不可用時 fallback 寫 structlog（不阻擋啟動）

**Files touched:**
- New: `src/hft_platform/ops/config_snapshot.py`（含 `ALLOWED_ENV_PREFIXES` + `REDACT_PATTERNS` 常數）
- New: `src/hft_platform/migrations/clickhouse/20260327_001_add_config_snapshots.sql`
- Edit: `src/hft_platform/services/bootstrap.py` (呼叫 snapshot)
- New: `tests/unit/test_config_snapshot.py`（必須包含 `test_secrets_are_redacted`）
- Edit: `docs/operations/long-term-risk-register.md` (R12 → ✅ Done)

**Verification:** 重啟後 `SELECT * FROM hft.config_snapshots ORDER BY boot_ts DESC LIMIT 1` 有資料，且 `env_json` 不含任何 password/token/key 值。

---

## Wave 2 — 上線後第一個月

目標：可持續運營 — log 可搜、磁碟不爆、硬體有監控、例行檢核不靠人記。

### Prerequisite: node-exporter textfile collector

Wave 2-B 和 2-C 都依賴 node-exporter textfile metrics，但目前 `docker-compose.yml` 的 node-exporter 只配置了 filesystem collector，**沒有 `--collector.textfile.directory`，也沒有 mount textfile 目錄**。

必須先完成：
- Host 上建立 `/var/lib/node-exporter/textfile/` 目錄
- `docker-compose.yml` node-exporter 加 volume mount：`/var/lib/node-exporter/textfile:/var/lib/node-exporter/textfile:ro`
- 加 command flag：`--collector.textfile.directory=/var/lib/node-exporter/textfile`
- 2-B 和 2-C 的 cron scripts 寫 `.prom` 檔案到此目錄

**Files touched:**
- Edit: `docker-compose.yml`（node-exporter volumes + command）

### 2-A. Loki + Promtail 集中式 log

**Problem:** 無集中式 log，出事只能 `docker logs` 逐 container 翻。`promtail.yml` 已存在但無 service。

**Solution:**
- `docker-compose.yml` 加 `loki` service（`grafana/loki:3.0`，local storage）
  - Retention: `168h` (7 天)
  - Named volume: `loki_data`
- `docker-compose.yml` 加 `promtail` service，mount 現有 `config/monitoring/promtail.yml` + `/var/lib/docker/containers`
- 不加 Grafana — 查 log 走 `logcli` CLI 或未來 monitor TUI 整合
- Prometheus alert rule: `loki_up == 0` → Telegram

**Files touched:**
- Edit: `docker-compose.yml` (add loki + promtail services)
- Edit: `config/monitoring/promtail.yml` (verify target matches)
- New: `config/monitoring/loki.yml` (Loki local config)
- Edit: `config/monitoring/alerts/rules.yaml` (add loki_up alert)

**Verification:** `logcli query '{container="hft-engine"}' --limit 10` 回結果。

### 2-B. Research data rotation 自動化

**Problem:** `research/data/` ~44 GB/月增長，12-18 個月磁碟爆滿（Risk register R04）。

**Scope:** 依 `docs/operations/data-retention-policy.md` 的分類，rotation 覆蓋所有四類目錄：

| Path | Policy |
|------|--------|
| `research/data/raw/` | 保留 90 天，超齡壓縮至 archive，archive 超 180 天刪除 |
| `research/data/processed/<alpha_id>/` | 僅保留 Gate B 以上 active alpha 的資料；inactive alpha 目錄超 90 天刪除 |
| `research/data/synthetic/` | 僅保留最新版本，舊版本直接刪除 |
| `research/experiments/runs/` | 保留 90 天；scorecard 已 promote 至 ClickHouse 的可安全刪除 |

**Solution:**
- `scripts/research_data_rotate.sh`:
  - 分四段處理上述四類目錄
  - 可配置：`RESEARCH_RAW_RETAIN_DAYS=90`, `RESEARCH_ARCHIVE_RETAIN_DAYS=180`, `RESEARCH_RUNS_RETAIN_DAYS=90`
  - `--dry-run` 模式（只印不刪）
  - 保護 `research/data/processed/smoke/smoke_v1.npy`（data-retention-policy 指定的 must-keep）
- Cron: `0 4 * * 0`（每週日 04:00）
- node-exporter textfile gauge: `hft_research_data_bytes`（寫到 `/var/lib/node-exporter/textfile/research_data.prom`）
- Prometheus alert: `hft_research_data_bytes > 200e9` → Telegram

**Files touched:**
- New: `scripts/research_data_rotate.sh`
- Edit: `docs/operations/cron-setup-remote.md` (add cron entry)
- Edit: `config/monitoring/alerts/rules.yaml` (add research_data alert)
- Edit: `docs/operations/long-term-risk-register.md` (R04 → ✅ Done)

**Verification:** 手動跑 `./scripts/research_data_rotate.sh --dry-run`，確認四類目錄都有處理且 smoke data 未被標記刪除。

### 2-C. SMART disk monitoring

**Problem:** Risk register R09 ⚠️ open。SSD 磨損無監控。

**Solution:**
- 部署前提：`sudo apt install smartmontools`
- `scripts/smart_check.sh`:
  - `smartctl -A /dev/sda` → 解析 `Reallocated_Sector_Ct`, `Wear_Leveling_Count`
  - 寫 Prometheus textfile 到 `/var/lib/node-exporter/textfile/smartmon.prom`
- Cron: `0 5 * * 1`（每週一 05:00）
- Prometheus alert: `smartmon_reallocated_sectors > 100` → Telegram

**Files touched:**
- New: `scripts/smart_check.sh`
- Edit: `docs/operations/cron-setup-remote.md` (add cron + install note)
- Edit: `config/monitoring/alerts/rules.yaml` (add SMART alert)
- Edit: `docs/operations/long-term-risk-register.md` (R09 → ✅ Done)

### 2-D. 季度檢核自動化

**Problem:** Quarterly checklist 全手動（TTL、Prometheus storage、OS updates、SMART、SDK pin）。

**Solution:**
- `scripts/quarterly_health_check.py`:
  - ClickHouse TTL 驗證：`SELECT count() FROM hft.market_data WHERE toDateTime(ingest_ts / 1000000000) < now() - INTERVAL 6 MONTH` 應為 0（TTL 基於 `ingest_ts`，retention 6 個月）
  - Prometheus storage：query `prometheus_tsdb_storage_size_bytes`
  - OS updates：`apt list --upgradable 2>/dev/null | wc -l`
  - SMART：呼叫 `smart_check.sh`
  - Shioaji SDK：比對 `uv.lock` 中鎖定版本與 `pyproject.toml` pin 是否一致（不用 `pip index`，本 repo 用 `uv` 管理）
  - 輸出 JSON report + Telegram summary（PASS/WARN/FAIL per item）
- Cron: `0 7 1 1,4,7,10 *`（每季第一天 07:00）
- Makefile target: `make quarterly-health-check`

**Files touched:**
- New: `scripts/quarterly_health_check.py`
- Edit: `Makefile` (add target)
- Edit: `docs/operations/cron-setup-remote.md` (add cron)

---

## Wave 3 — 持續強化（TCA + Live Feasibility 補完）

目標：量化交易品質，建立 feasibility scorecard。

**重要：本波是「補完既有計畫」，不是從零開始。** 以下已存在的工作不重做：

### 已存在（不重做）

| Item | Status | Location |
|------|--------|----------|
| `tca/types.py` (`SlippageBreakdown`, `TCADailyReport`, `FeeSchedule`, `FeeBreakdown`) | ✅ Done | `src/hft_platform/tca/` |
| `tca/fee_calculator.py` (`FeeCalculator`) | ✅ Done | `src/hft_platform/tca/` |
| `tca/analyzer.py` (`TCAAnalyzer`) | ✅ Done | `src/hft_platform/tca/` |
| `cli/_tca.py` (`hft tca daily`) | ✅ Done | `src/hft_platform/cli/` |
| `OrderIntent.decision_mid`, `OrderIntent.decision_price` | ✅ Done | `contracts/strategy.py:59-63` |
| `OrderCommand.decision_price`, `OrderCommand.arrival_price` | ✅ Done | `contracts/strategy.py:89-90` |
| `hft.slippage_records` table | ✅ Done | `migrations/clickhouse/20260325_001_add_slippage_records.sql` |
| `DailyLossLimitValidator` watermark | ✅ Done | tested |
| `services/daily_report.py` (`DailyReportService`) | ✅ Done | EOD report via SessionGovernor callback → Telegram |

### 3-A. TCA 模組補完（延伸既有 TCA package）

**缺少：**

| Module | Purpose |
|--------|---------|
| `tca/slippage.py` | `SlippageDecomposer` — 4 組件拆解（delay, market impact, timing, residual）。使用 `slippage_records` 表中已有的 `decision_mid`, `fill_price`, `latency_ns` |
| `tca/impact.py` | `SqrtImpactModel` — sqrt-volume impact 預測 |
| `tca/report.py` | `TCAReportGenerator` — 日/週彙總。**整合**（不取代）`DailyReportService`：TCA report 作為 daily report 的一個 section 附加，由 `DailyReportService` 呼叫 |

**資料合約：**
- `contracts/execution.py`: `FillEvent` 加 `decision_price: int = 0`, `arrival_price: int = 0`（目前 FillEvent 尚無這兩欄位，但 OrderCommand 已有，需要在 fill 路徑 passthrough）
- Migration: `20260327_002_add_tca_columns_to_fills.sql` — `hft.fills` 表加 `decision_price Int64 DEFAULT 0`, `arrival_price Int64 DEFAULT 0`

**Tests:** `test_tca_slippage.py`, `test_tca_impact.py`, `test_tca_report.py`

### 3-B. Live Feasibility Validation 補完（延伸既有計畫）

**缺少：**

| Module | Purpose |
|--------|---------|
| `execution/slippage_tracker.py` | Per-fill slippage 即時計算 → Prometheus `hft_fill_slippage_bps` |
| `risk/liquidity_gate.py` | Spread > N pts → reject order（可配置 threshold） |
| `ops/daily_pnl_report.py` | EOD PnL 彙總。**整合**入 `DailyReportService` 作為 PnL section，不另建第二套 EOD 報表 |
| `analytics/__init__.py` + `analytics/queries.py` | ClickHouse 聚合 query：daily_pnl, slippage_distribution, fill_quality, liquidity_gate_stats |
| `cli/_feasibility.py` | `hft feasibility report` 子命令（調用 `analytics/queries.py`） |

**Migrations:**
- `daily_reports` 表（EOD 彙總持久化）
- `liquidity_gate_events` 表（gate rejection 記錄）
- （`slippage_records` 已存在，不重建）

**Daily Report 整合原則：**
- `DailyReportService` 是唯一的 EOD 報表入口（已由 SessionGovernor CLOSED callback 觸發）
- `TCAReportGenerator` 和 `daily_pnl_report` 作為 report sections 被 `DailyReportService` 呼叫
- 不建立第二套 EOD Telegram 通知

**Tests:** `test_slippage_tracker.py`, `test_liquidity_gate.py`, `test_daily_pnl_report.py`

### 3-A/3-B 實作順序

```
FillEvent 合約擴充 (passthrough decision_price, arrival_price from OrderCommand)
  ├→ tca/slippage.py (uses existing slippage_records schema)
  │    └→ execution/slippage_tracker.py
  │         └→ tca/report.py (integrates into DailyReportService)
  │              └→ ops/daily_pnl_report.py (integrates into DailyReportService)
  ├→ tca/impact.py (parallel, standalone)
  └→ risk/liquidity_gate.py (parallel, standalone)
      └→ analytics/ + cli/_feasibility.py
```

---

## Success Criteria

| Wave | Done When |
|------|-----------|
| Wave 1 | Telegram 收到 test alert + SDK pinned to 1.2.9 + config snapshot 寫入 ClickHouse（無機密） |
| Wave 2 | `logcli` 可查 log + research data cron 覆蓋 4 類目錄 + SMART `.prom` 出現在 textfile dir + quarterly check PASS |
| Wave 3 | `hft feasibility report` 輸出完整 scorecard + slippage metric 出現在 Prometheus + DailyReportService 含 TCA/PnL sections |

## Risk Register Updates

完成後應更新的 risk register 項目：
- R04 (research data) → ✅ Done
- R09 (SMART) → ✅ Done
- R10 (Shioaji SDK) → ✅ Done
- R12 (config snapshot) → ✅ Done
