# HFT Platform

台灣市場（TAIFEX 期貨／選擇權、TWSE）的事件驅動高頻交易平台：Shioaji／Fubon 雙券商、
ClickHouse、Prometheus、Rust（PyO3）熱路徑核心。這個 repo 涉及真實資金，也有一個受治理的
研究計畫（`research/`）：研究產出不會直接啟用實盤，必須經過 Gate、Canary、Shadow。

> 實盤引擎目前凍結在 `r47_tmf_v1`（loop_v1 L11），見
> [docs/loop_v1_stabilization_charter.md](docs/loop_v1_stabilization_charter.md)。

## 架構

```
Exchange -> BrokerFacade(Shioaji|Fubon) -> Normalizer -> LOBEngine -> FeatureEngine
  -> RingBufferBus -> StrategyRunner -> RiskEngine -> GatewayService -> OrderAdapter -> BrokerFacade
                                   \-> RecorderService -> WAL / ClickHouse
```

完整流程圖、契約鏈（`OrderIntent -> RiskDecision -> OrderCommand -> FillEvent -> PositionDelta`）
與五大法則在 [AGENTS.md](AGENTS.md)；模組索引在 [docs/MODULES_REFERENCE.md](docs/MODULES_REFERENCE.md)，
架構基線在 [docs/architecture/current-architecture.md](docs/architecture/current-architecture.md)。

## 快速啟動（本機模擬）

```bash
uv sync --dev                 # 安裝依賴
cp .env.example .env          # 本機環境檔（不要提交，不要貼出內容）
uv run hft config build --list config/symbols.list --output config/symbols.yaml
uv run hft run sim            # 模擬模式
curl -fsS http://localhost:9090/metrics | head
```

`hft` 不在 PATH 時用 `uv run hft ...`。本機完整堆疊（ClickHouse、Redis、Prometheus…）用
`make start` / `make stop` / `make logs`。

**實盤與正式機**：`HFT_ORDER_MODE=live` 是真錢；正式機（THESHOW）的部署程序與本機完全不同
（不可 `git pull`、不可 `up -d`、不可 `docker compose restart`），一律照
[docs/runbooks/deployment.md](docs/runbooks/deployment.md) 並逐批取得人工核准。券商：預設
Shioaji；Fubon 設 `HFT_BROKER=fubon` 與 `HFT_FUBON_*`。

## 常用命令

`make help` 列出全部目標；以下是最常用的。

| 目的 | 命令 |
|------|------|
| 安裝／編譯 Rust | `make dev`、`make build-rust` |
| 測試 | `make test`、`make test-all`、`make test-file FILE=...`、`make coverage` |
| 品質 | `make lint`、`make typecheck`、`make discipline`、`make dependency-boundary` |
| 合併前 | `make check`、`make ci` |
| 運維檢查 | `make pre-market-check`、`make post-market-check`、`make recorder-status` |
| 效能 | `make benchmark`、`make hotpath-profile`、`make benchmark-compare` |
| 演練 | `make drill-ck-down`、`make drill-wal-pressure`、`make drill-recon-mismatch` |
| 研究 | `make research ALPHA=<id> OWNER=<you> DATA='<path>'`、`make research-scaffold ALPHA=<id>` |
| Agent 文件檢查 | `make agent-docs-check`、`make roadmap-delivery-check` |

測試規範：新程式碼 ≥80% 覆蓋率、熱路徑 ≥90%（全域門檻 70%）；命名 `test_<behavior>_<scenario>`；
每個測試都要有 `assert`；不用固定 sleep。細節見 [AGENTS.md](AGENTS.md)。

## 研究 Pipeline

```
論文 -> 原型 -> 資料 -> 回測(延遲+成本) -> 統計驗證 -> 參數優化 -> Paper trade -> Live(Rust)
```

Gate A–F（Manifest／測試／回測／晉升門檻／Paper trade／Rust readiness）與 Canary 的定義和門檻在
[docs/runbooks/alpha-development-workflow.md](docs/runbooks/alpha-development-workflow.md)；工廠操作手冊是
[research/README.md](research/README.md)。入口：`make research ALPHA=<id> OWNER=<you> DATA='<path>'`。

## 設定與環境變數

設定依序覆蓋：`config/base/main.yaml` → `config/env/<mode>/main.yaml` → `config/settings.py`（本機）→
`HFT_*` 環境變數 → CLI。完整環境變數表（有檢查腳本守護）：
[docs/operations/env-vars-reference.md](docs/operations/env-vars-reference.md)。金鑰只放 `.env` 或環境變數，
不進程式碼、日誌、命令列參數或提交。

## 文件地圖

| 類別 | 文件 |
|------|------|
| 新手入門 | [docs/guides/getting-started.md](docs/guides/getting-started.md) |
| CLI／設定／策略／特徵 | [cli-reference](docs/guides/cli-reference.md)、[config-reference](docs/guides/config-reference.md)、[strategy-guide](docs/guides/strategy-guide.md)、[feature-guide](docs/guides/feature-guide.md) |
| 架構與模組 | [current-architecture](docs/architecture/current-architecture.md)、[MODULES_REFERENCE](docs/MODULES_REFERENCE.md) |
| 部署（正式機） | [docs/runbooks/deployment.md](docs/runbooks/deployment.md) |
| 從零建置堆疊 | [docs/operations/deployment.md](docs/operations/deployment.md) |
| 變更管理 | [docs/operations/change-control.md](docs/operations/change-control.md) |
| Runbooks／排錯 | [docs/runbooks/README.md](docs/runbooks/README.md)、[troubleshooting](docs/operations/troubleshooting.md) |
| 研究 | [research/README.md](research/README.md)、[research/SOP.md](research/SOP.md) |
| 路線圖 | [ROADMAP.md](ROADMAP.md) |
| 文件總索引 | [docs/README.md](docs/README.md) |

## AI agent 設定

Claude Code、Codex、Copilot 共用同一份規則，單一來源是 [AGENTS.md](AGENTS.md)：

| 層級 | 檔案 | 說明 |
|------|------|------|
| 專案規則 | `AGENTS.md` | 架構、五大法則、紅線、完成標準、回報格式；`CLAUDE.md` 以 `@AGENTS.md` 匯入並補 Claude 專屬說明 |
| 子樹規則 | `rust_core/`、`tests/`、`research/` 的 `AGENTS.md` | 只寫該目錄與全域不同之處 |
| Skills | `.agent/skills/<name>/SKILL.md` | 31 個專案技能；`.claude/skills`、`.agents/skills` 是指向它的連結，兩個工具原生發現 |
| 依需讀取的規則 | `.agent/rules/` | git、部署（D1–D10）、並行 agent、無人值守、研究資料 |
| Subagents | `.claude/agents/` | executor／reviewer／test-writer／docs |
| 強制層 | `.claude/settings.json` 與 `.claude/hooks/` | 權限的 ask／deny 與 hooks 實際擋下紅線操作 |
| 記憶 | `.agent/memory/` | 已知陷阱、風險、失敗嘗試、委派紀錄 |

`make agent-docs-check` 檢查文件路徑與 skill 前置資料。修改這些檔案的原則見
`.agent/skills/agent-meta-audit/references/prompt-principles.md`。
