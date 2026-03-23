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

| 命令 | 說明 |
|------|------|
| `make dev` | 安裝開發依賴 |
| `make build-rust` | 編譯 Rust 擴展 (maturin) |
| `make test` | 跑 unit tests |
| `make test-all` | Unit + integration tests |
| `make coverage` | 測試覆蓋率 (最低 70%) |
| `make lint` | Ruff linter |
| `make typecheck` | mypy 型別檢查 |
| `make discipline` | AST 紀律檢查 (9 rules) |
| `make check` | 全部品質閘門 |
| `make start` | Docker Compose 啟動 |
| `make stop` | Docker Compose 停止 |
| `make benchmark` | 效能基準測試 |
| `make hotpath-profile` | Hot path 延遲分析 |
| `make research ALPHA=<id> OWNER=<owner> DATA='<path>'` | Alpha 研究 pipeline (Gate A-E) |
| `make research-scaffold ALPHA=<id>` | 建立新 alpha 骨架 |
| `make help` | 顯示所有 Makefile 目標 |
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

| 變數 | 預設 | 說明 |
|------|------|------|
| `HFT_MODE` | `sim` | 執行模式: `sim` / `live` / `replay` |
| `HFT_BROKER` | `shioaji` | 券商: `shioaji` / `fubon` |
| `HFT_SYMBOLS` | — | 逗號分隔 symbol 清單 |
| `HFT_GATEWAY_ENABLED` | `0` | `1` = 啟用 order/risk gateway |
| `HFT_RECORDER_MODE` | `direct` | `wal_first` = WAL 優先寫入 |
| `HFT_FEATURE_ENGINE_ENABLED` | `1` | `0` = 停用 FeatureEngine |
| `HFT_FUSED_NORMALIZER` | `0` | `1` = Rust fused normalizer+LOB |
| `HFT_STORMGUARD_FEED_GAP_HALT_S` | `30` | Feed gap 觸發 HALT 秒數 |
| `SHIOAJI_API_KEY` | — | Shioaji API Key |
| `SHIOAJI_SECRET_KEY` | — | Shioaji Secret Key |

完整清單: [docs/operations/env-vars-reference.md](docs/operations/env-vars-reference.md)
<!-- END AUTO-GENERATED: env-table -->

## 專案結構

```
src/hft_platform/     Runtime 核心 (29 packages, ~55k LOC)
├── feed_adapter/     多券商市場資料 (Shioaji + Fubon)
├── feature/          FeatureEngine (16 LOB 衍生特徵)
├── strategy/         策略執行器 + circuit breaker
├── risk/             風險引擎 + StormGuard FSM
├── order/            訂單轉譯 + rate limiting
├── execution/        成交路由 + 部位追蹤
├── recorder/         ClickHouse + WAL 持久化
├── alpha/            Alpha 治理 pipeline (Gate A-F)
├── monitor/          Signal Monitor TUI
├── gateway/          Order/Risk gateway
├── observability/    Prometheus metrics
└── services/         Bootstrap + 服務編排

rust_core/            Rust 擴展 (PyO3, ~6k LOC)
config/               YAML 設定 + 環境覆蓋
research/             研究工廠 (102+ alpha, SOP pipeline)
tests/                Unit / Integration / Benchmark (485 files)
docs/                 架構 / 運維 / Runbooks
.agent/               AI 規則 / Skills / Memory
```

## 文件入口

| 類別 | 文件 |
|------|------|
| 速查表 | [docs/AI_DEVELOPER_CHEAT_SHEET.md](docs/AI_DEVELOPER_CHEAT_SHEET.md) |
| 新手入門 | [docs/getting_started.md](docs/getting_started.md) |
| CLI 參考 | [docs/cli_reference.md](docs/cli_reference.md) |
| 設定參考 | [docs/config_reference.md](docs/config_reference.md) |
| 部署指南 | [docs/deployment_guide.md](docs/deployment_guide.md) |
| 架構基線 | [docs/architecture/current-architecture.md](docs/architecture/current-architecture.md) |
| 運維手冊 | [docs/runbooks.md](docs/runbooks.md) |
| 策略開發 | [docs/strategy-guide.md](docs/strategy-guide.md) |
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
