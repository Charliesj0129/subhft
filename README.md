# HFT Platform

事件驅動高頻交易平台 — Shioaji / Fubon 雙券商 + ClickHouse + Prometheus + Rust 擴展。

## 架構概覽

```
Exchange → BrokerFacade(Shioaji|Fubon) → Normalizer → LOBEngine → FeatureEngine
    → RingBufferBus → StrategyRunner → RiskEngine → OrderAdapter → BrokerFacade
                                          ↘ RecorderService → WAL / ClickHouse
```

**7 Runtime Planes**: Control · Market Data · Feature · Decision · Execution · Persistence · Observability

**Tech Stack**: Python 3.12 + Rust (PyO3 `rust_core`) + ClickHouse + Prometheus

## 快速啟動（本機模擬）

```bash
# 1) 安裝依賴
uv sync --dev

# 2) 建立本機環境檔
cp .env.example .env

# 3) 由 symbols.list 生成 symbols.yaml
uv run hft config build --list config/symbols.list --output config/symbols.yaml

# 4) 啟動模擬
uv run hft run sim

# 5) 驗證 metrics
curl -fsS http://localhost:9090/metrics | head
```

> 若 `hft` 不在 PATH，使用 `uv run hft ...` 或 `python -m hft_platform ...`。

## Docker Compose（單機部署）

```bash
# 資料服務
docker compose up -d clickhouse redis

# 主服務與觀測
docker compose up -d --build hft-engine prometheus alertmanager hft-monitor

# 看主流程日誌
docker compose logs -f hft-engine
```

| 服務 | 端口 | 健康檢查 |
|------|------|---------|
| hft-engine | 9090 | `/metrics` |
| ClickHouse | 8123 (HTTP), 9000 (native) | `SELECT 1` |
| Redis | 6379 | `redis-cli ping` |
| Prometheus | 9091 | `/-/healthy` |
| Alertmanager | 9093 | `/-/healthy` |

```bash
docker compose down   # 停止
```

## Live 模式

```bash
export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
export HFT_MODE=live
uv run hft run live
```

若缺 `SHIOAJI_API_KEY/SHIOAJI_SECRET_KEY`，CLI 會自動降級為 `sim`。

Fubon 券商：設定 `HFT_BROKER=fubon` + `HFT_FUBON_*` 環境變數。

<!-- AUTO-GENERATED: commands-table -->
## 常用命令

### 開發

| 命令 | 說明 |
|------|------|
| `make dev` | 安裝開發依賴 |
| `make build-rust` | 編譯 Rust 擴展 (maturin) |
| `make help` | 顯示全部 139 個 Makefile 目標 |

### 測試與品質

| 命令 | 說明 |
|------|------|
| `make test` | 跑 unit tests |
| `make test-all` | Unit + integration tests |
| `make test-file FILE=...` | 跑單一測試檔 (無 coverage gate) |
| `make coverage` | 測試覆蓋率 (最低 70%) |
| `make lint` | Ruff linter |
| `make typecheck` | mypy 型別檢查 |
| `make discipline` | AST 紀律檢查 (9 rules) |
| `make check` | lint + typecheck + discipline + dependency-boundary + test-hygiene |
| `make ci` | 完整 CI pipeline (format-check + lint + typecheck + coverage) |
| `make security-audit` | 依賴安全掃描 |

### 部署與運維

| 命令 | 說明 |
|------|------|
| `make start` | Docker Compose 啟動 |
| `make start-engine` | 啟動 HFT engine + 核心基礎設施 |
| `make stop` | Docker Compose 停止 |
| `make logs` | 顯示 hft-engine 日誌 |
| `make pre-market-check` | 盤前健康檢查 (Docker, CK, Redis, WAL) |
| `make post-market-check` | 盤後健康檢查 (WAL, recorder, PnL) |
| `make recorder-status` | 顯示 WAL backlog 與 ClickHouse 狀態 |
| `make canary-auto` | One-shot canary gate (snapshot + wait + evaluate) |

### 效能

| 命令 | 說明 |
|------|------|
| `make benchmark` | 效能基準測試 |
| `make hotpath-profile` | Hot path 延遲分析 (normalizer→LOB→feature→strategy→risk) |
| `make benchmark-baseline` | 產生 baseline for Darwin Gate |
| `make latency-gate-ci` | CI 延遲回歸檢查 |

### 研究與 Alpha

| 命令 | 說明 |
|------|------|
| `make research ALPHA=<id> OWNER=<owner> DATA='<path>'` | Alpha 研究 pipeline (Gate A-E) |
| `make research-scaffold ALPHA=<id>` | 建立新 alpha 骨架 |
| `make research-report ALPHA=<id>` | 渲染 promotion 報告 |
| `make research-fetch-paper ARXIV=<id>` | 抓取 arXiv 論文 |

### 演練 (Drills)

| 命令 | 說明 |
|------|------|
| `make drill-ck-down` | ClickHouse 停機 30s 演練 (WAL fallback) |
| `make drill-wal-pressure` | 磁碟壓力演練 |
| `make drill-recon-mismatch` | 對帳不符演練 |
| `make rollback-drill` | Rollback 程序演練 |
<!-- END AUTO-GENERATED: commands-table -->

## 測試與品質

```bash
make test              # Unit tests
make test-all          # Unit + integration
make coverage          # Coverage (≥70% line, ≥55% branch)
make check             # lint + typecheck + discipline + test-hygiene
make ci                # 完整 CI pipeline (format-check + lint + typecheck + coverage)
```

測試規範：
- 新程式碼 ≥80% 覆蓋率，hot path (normalizer, lob, risk) ≥90%
- 測試命名: `test_<behavior>_<scenario>`，禁止 `test_covers_*`
- 所有測試必須有 `assert`（零 assertion 上限: 30）
- Sleep ≤50ms，優先使用 `threading.Event` / `asyncio.Event`

## Agent Teams

三個預設 AI 協作團隊，一行指令啟動（需 Claude Code v2.1.32+）：

```
/alpha-research OFI 類型           # Alpha 研發（三角牽制）
/code-review-team staged changes   # 多維度 Code Review
/debug-team <症狀描述>              # 跨 Runtime Plane 除錯
```

詳見 [docs/agent-teams/README.md](docs/agent-teams/README.md)

## Alpha 研究 Pipeline

```
論文(MCP arXiv) → 原型(Python) → 資料 → 回測(延遲+成本) → 統計驗證 → 參數優化 → Paper Trade → Live(Rust)
```

| Gate | 檢查 | 模組 |
|------|------|------|
| A | Manifest + 資料欄位 + 複雜度 | `alpha/validation.py` |
| B | pytest 正確性 | `alpha/validation.py` |
| C | 回測 + 統計 + 參數穩健性 | `alpha/validation.py` |
| D | Sharpe/DD 閾值 + 組合關聯 | `alpha/promotion.py` |
| E | Shadow trading + 執行品質 | `alpha/promotion.py` |
| Canary | 漸進放量 + 自動 rollback | `alpha/canary.py` |

入口: `make research ALPHA=<id> OWNER=<owner> DATA='<path>'`，SOP: [research/SOP.md](research/SOP.md)

<!-- AUTO-GENERATED: env-table -->
## 關鍵環境變數

### 核心

| 變數 | 預設 | 說明 |
|------|------|------|
| `HFT_MODE` | `sim` | 執行模式: `sim` / `live` / `replay` |
| `HFT_ORDER_MODE` | `sim` | 訂單模式: `sim` / `live` (**live = 真錢**) |
| `HFT_BROKER` | `shioaji` | 券商: `shioaji` / `fubon` |
| `HFT_SYMBOLS` | — | 逗號分隔 symbol 清單 |
| `SHIOAJI_API_KEY` | — | Shioaji API Key |
| `SHIOAJI_SECRET_KEY` | — | Shioaji Secret Key |

### 架構開關

| 變數 | 預設 | 說明 |
|------|------|------|
| `HFT_GATEWAY_ENABLED` | `0` | `1` = 啟用 CE-M2 order/risk gateway |
| `HFT_RECORDER_MODE` | `direct` | `wal_first` = WAL 優先寫入 (CE-M3) |
| `HFT_FEATURE_ENGINE_ENABLED` | `1` | `0` = 停用 FeatureEngine (27 features v3) |
| `HFT_FUSED_NORMALIZER` | `0` | `1` = Rust fused normalizer+LOB (20-30x) |
| `HFT_FEATURE_ENGINE_BACKEND` | `python` | `rust` = Rust feature kernel |

### 安全與監控

| 變數 | 預設 | 說明 |
|------|------|------|
| `HFT_STORMGUARD_FEED_GAP_HALT_S` | `30` | Feed gap 觸發 HALT 秒數 |
| `HFT_RECONNECT_HOURS` | `08:30-13:35` | 自動重連交易時段 |
| `HFT_MONITOR_LIVE_ENABLED` | `0` | `1` = 啟用 Redis live publisher |
| `HFT_TELEGRAM_ENABLED` | `0` | `1` = 啟用 Telegram 通知 |
| `HFT_STARTUP_RECON_ENABLED` | `1` | 啟動部位恢復 |
| `HFT_CHECKPOINT_ENABLED` | `1` | 定期部位 checkpoint |

完整清單 (60+ 變數): [docs/operations/env-vars-reference.md](docs/operations/env-vars-reference.md)
<!-- END AUTO-GENERATED: env-table -->

## 專案結構

```
src/hft_platform/        Runtime 核心 (37 packages, ~210 files, ~44k LOC)
├── feed_adapter/        多券商市場資料 (Shioaji 20 files + Fubon 14 files)
│   ├── shioaji/         Shioaji 完整子包 (session/quote/order/account/contracts)
│   ├── fubon/           Fubon 完整子包
│   └── _base/           共用基類 (session_runtime, quote_watchdog, cooldown)
├── feature/             FeatureEngine v3 (27 LOB 衍生特徵, Python/Rust dual)
├── engine/              RingBufferBus (3 modes: python/rust_pyobj/rust_typed)
├── strategy/            策略 SDK + StrategyRunner + circuit breaker
├── strategies/          7 core + 5 alpha 策略實作
├── risk/                風險引擎 (10 files) + StormGuard FSM + validators
├── order/               訂單 dispatch + rate limiting + shadow mode
├── execution/           成交路由 + 部位追蹤 + 對帳 + execution optimizer (15 files)
├── gateway/             CE-M2 Order/Risk gateway (optional)
├── recorder/            ClickHouse + WAL 持久化 (22 files, dual mode)
├── services/            Bootstrap + 服務編排 (11 files, 18+ services)
├── config/              5 層 config merge + symbol DSL + hot reload
├── core/                timebase, pricing, order_ids, market_calendar
├── alpha/               Alpha 治理 pipeline Gate A-F (18+ files)
├── ops/                 運維 (14 files: session governor, autonomy, flattener)
├── monitor/             Signal Monitor TUI (19 files, CK+Redis dual source)
├── observability/       Prometheus 100+ metrics + health server
├── notifications/       Telegram + Webhook + AlertManager
├── reports/             每日市場報告 pipeline (collector→reasoner→composer)
├── tca/                 Transaction Cost Analysis
├── options/             選擇權定價 + Greeks + IV surface
├── bot/                 Telegram Bot
├── backtest/            HftBacktest 整合
└── ...                  analytics, data_quality, diagnostics, ipc, testing, utils

rust_core/               Rust 擴展 (PyO3, 36 pyclass + 22 pyfunction)
config/                  YAML 設定 + 環境覆蓋
research/                研究工廠 (8 surviving alpha, SOP pipeline)
tests/                   Unit / Integration / Benchmark (322 files)
docs/                    架構 / 運維 / Runbooks (306 files)
.agent/                  AI 規則 / Skills / Memory
```

## 文件入口

| 類別 | 文件 |
|------|------|
| 速查表 | [docs/guides/ai-developer-cheat-sheet.md](docs/guides/ai-developer-cheat-sheet.md) |
| 新手入門 | [docs/guides/getting-started.md](docs/guides/getting-started.md) |
| CLI 參考 | [docs/guides/cli-reference.md](docs/guides/cli-reference.md) |
| 設定參考 | [docs/guides/config-reference.md](docs/guides/config-reference.md) |
| 策略開發 | [docs/guides/strategy-guide.md](docs/guides/strategy-guide.md) |
| Feature 指南 | [docs/guides/feature-guide.md](docs/guides/feature-guide.md) |
| 架構基線 | [docs/architecture/current-architecture.md](docs/architecture/current-architecture.md) |
| 模組參考 | [docs/MODULES_REFERENCE.md](docs/MODULES_REFERENCE.md) |
| 環境變數 | [docs/operations/env-vars-reference.md](docs/operations/env-vars-reference.md) |
| 部署指南 | [docs/operations/deployment.md](docs/operations/deployment.md) |
| Runbooks | [docs/runbooks/README.md](docs/runbooks/README.md) |
| 排錯指南 | [docs/operations/troubleshooting.md](docs/operations/troubleshooting.md) |
| Agent Teams | [docs/agent-teams/README.md](docs/agent-teams/README.md) |
| 研究 SOP | [research/SOP.md](research/SOP.md) |
| 路線圖 | [ROADMAP.md](ROADMAP.md) |
| 文件總索引 | [docs/README.md](docs/README.md) |

## HFT 五大法則

1. **Allocator Law**: Hot path 禁止 heap allocation — 用 pre-allocated buffers
2. **Cache Law**: Structure of Arrays > Array of Objects — 用 numpy / Rust Vec
3. **Async Law**: Event loop 禁止 blocking IO > 1ms
4. **Precision Law**: 金融計算禁止 float — 用 scaled int (x10000)
5. **Boundary Law**: Python↔Rust 必須 zero-copy interface
