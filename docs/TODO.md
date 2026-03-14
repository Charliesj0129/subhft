# 技術債與 TODO（僅保留未完成項）

更新日期：2026-03-14

> 本文件只追蹤「尚未落地」項目。已完成事項不再保留於此，請改查 git 歷史與對應 runbook / architecture 文檔。

> WS-G / WS-H 治理欄位標準：`技能`、`RACI`、`Agent 角色`、`KPI`、`風險與緩解`、`依賴`、`Gate 證據`。
> 固定角色字典：`Rust Lead`、`Tech Lead`、`Strategy Owner`、`Ops Oncall`、`Research Lead`、`Head of Research`、`Data Steward`、`Trading Runtime Owner`。

## 1. 架構與核心路徑

### 1.1 Feature Plane Productionization（P0）
- 狀態：進行中（Phase 18 已實作 FeatureEngine — 16 features, wired into StrategyContext/Runner/BacktestAdapter, feature-flagged `HFT_FEATURE_ENGINE_ENABLED=1`；production 化待完成）
- 追蹤文件：`docs/architecture/feature-engine-lob-research-unification-spec.md`
- 待辦：
  - Python FeatureEngine 已完成，剩餘工作為 production hardening：
  - 將 Rust feature kernel 從 prototype 提升到 production-ready（先聚焦已 promoted 的 feature family）。
  - 定稿 Python/Rust typed feature frame 邊界（packed/zero-copy transport contract）。
  - 完成 CI parity test（research/replay/live feature schema 一致性）。
  - Default-on rollout（目前 default off）。
- 驗收標準：
  - research/replay/live 三條路徑 feature schema 與 warmup/reset 規則一致。
  - parity 測試與回歸測試可在 CI 穩定通過。

### 1.2 Shioaji Adapter 後續收斂（P1）
- 狀態：進行中（code cutover 完成，待 production burn-in）
- 追蹤文件：`docs/architecture/shioaji-client-resilience-decoupling-plan.md`
- 待辦：
  - 完成 production burn-in 觀測窗口（連續交易日穩定性證據）。
  - 收斂殘留 low-risk legacy shim，降低 facade 層長期維護成本。
- 驗收標準：
  - 連續觀測期內無 reconnect storm / callback crash signature。
  - 相關 SLO 與告警在月度審查包內可稽核。

### 1.3 CE2/CE3 營運化追蹤（P1）
- 狀態：持續營運
- 追蹤文件：`docs/architecture/cluster-evolution-backlog.md`
- 待辦：
  - 按季度執行 gateway / wal-first chaos 子集並附掛證據到 reliability review。
  - 針對 backlog/replay 健康度維持月度趨勢審核（非一次性開發項）。

### 1.4 熱路徑 Rust 化擴編（P0）
- 狀態：規劃中（已有 `rust_core` 基礎，尚未覆蓋更多 execution hotpath）
- 追蹤文件：`docs/architecture/rust_pyo3.md`, `docs/architecture/rust_pyo3_typed_ring_migration.md`
- 技能：`hft-strategy-dev`、`rust_feature_engineering`、`performance-profiling`
- RACI：R=Rust Lead、A=Tech Lead、C=Strategy Owner、I=Ops Oncall
- Agent 角色：`explorer`（profiling/baseline，輸出 `hotpath_matrix`）→ `worker`（Rust cutover/CI，輸出 `cutover_patch+ci_report`）→ `default`（整合驗收，輸出 `gate_summary`）
- KPI：
  - end-to-end `p95 latency` 較 2026-03 基線下降 >= 20%。
  - `FFI copy ratio` <= 5%，`alloc/tick` 較基線下降 >= 30%。
  - `parity pass rate = 100%`（核心契約：`int x10000`）。
  - 連續 30 天 soak 期間 `parity_critical=0`。
- 風險與緩解：
  - 風險：Python↔Rust 邊界產生隱性拷貝，導致尾延遲惡化。
  - 緩解：先完成 typed frame/zero-copy contract 掃描，再允許 cutover 進 CI gate。
  - 風險：價格精度或事件語義回歸。
  - 緩解：強制 parity test + replay regression，違反即 block promote。
  - 風險：Rust kernel 佔用 GIL 或引發 CPU 競爭。
  - 緩解：在 profiling 報告中納入 GIL 與 core contention 指標，未達標不進 soak。
- 依賴：
  - 流程依賴：`profiling matrix -> kernel cutover -> CI parity/perf gate -> soak`。
  - 工作流依賴：依賴 WS-A 的 burn-in 量測輸出與 WS-B 的 recorder 吞吐基線。
- Gate 證據：
  - `hotpath_matrix`、before/after latency 報表、FFI/alloc 指標、parity 測試報告、30 天 soak 摘要、RACI owner 簽核紀錄。
- 待辦：
  - 建立 tick→intent→order→fill 全鏈路 hotpath profiling matrix，定義分批 Rust 化優先序。
  - 第一批切分 `strategy/risk/order/execution` 純計算熱點為 Rust kernels，並保留 typed binding 邊界一致性。
  - 增設 Python↔Rust FFI latency/alloc guardrail 與 parity gate（CI + soak 測試）。
- 驗收標準：
  - 目標熱路徑的 end-to-end p95 latency 較基線下降 >= 20%。
  - 連續 soak 期間無價格精度/事件語義回歸（`int x10000` 契約不破壞）。

## 2. 研究與分析工廠

### 2.1 Alpha Scaffold 檢索能力升級（P2）
- 位置：`research/tools/alpha_scaffold.py`, `research/tools/fetch_paper.py`
- 待辦：
  - citation profile 由規則表升級為 parser + embedding 的混合檢索流程。

### 2.2 論文引用資料補齊（P2）
- 位置：`research/knowledge/notes/`
- 待辦：
  - 修復 citation audit 缺漏（目前仍有 `missing_any=93` 類型缺口）。
  - 補齊 arXiv/作者/發佈資訊來源並建立批次校驗流程。

### 2.3 熱點效能優化（P2）
- 位置：`research/knowledge/reports/root_reports/*.svg`
- 待辦：
  - 依 pyspy triage 優先處理 `lob_engine.py` 熱點。
  - 收斂 import/config warmup 開銷並提供 before/after 量測證據。

### 2.4 研究與分析工廠擴容（P1）
- 位置：`research/`, `research/knowledge/`, `research/tools/`
- 技能：`hft-alpha-research`、`validation-gate`、`clickhouse-io`
- RACI：R=Research Lead、A=Head of Research、C=Data Steward、I=Trading Runtime Owner
- Agent 角色：`explorer`（source inventory，輸出 `source_catalog`）→ `worker`（pipeline/quality gate，輸出 `factory_pipeline+quality_report`）→ `default`（報告與 promotion 整合，輸出 `promotion_readiness`）
- KPI：
  - 每週候選研究處理量 >= 50 篇，引用完整率 >= 98%。
  - 去重命中率 >= 95%，research->alpha scaffold 中位 lead time <= 2 天。
  - promotion 前 quality gate 通過率（月）>= 90%。
- 風險與緩解：
  - 風險：來源 metadata 品質不穩，導致引用與去重失真。
  - 緩解：建立來源分級與抽樣稽核，未達門檻來源先隔離。
  - 風險：批次分析失敗堆積，產能下降。
  - 緩解：建立 batch retry 上限與 backlog 告警，超閾值切入人工 triage。
  - 風險：hypothesis queue 品質波動造成 promotion 噪音。
  - 緩解：在 promotion 前增加 quality gate（可重現性/時效性）與 RACI A 角色簽核。
- 依賴：
  - 流程依賴：`source inventory -> metadata/dedup -> batch analysis -> hypothesis queue -> promotion pre-check`。
  - 工作流依賴：依賴 WS-C 的資料品質稽核輸出與 Gate A-E 驗證管線。
- Gate 證據：
  - `source_catalog`、每週 throughput/quality 報表、dedup/citation audit、lead time 趨勢、promotion pre-check 結果、RACI owner 簽核紀錄。
- 待辦：
  - 擴充多來源研究輸入（論文/技術文章/實務報告）並統一 metadata 與去重規則。
  - 建立批次化分析管線（topic clustering、citation graph、alpha hypothesis queue）。
  - 建立工廠產能儀表（每週處理量、引用完整率、research→alpha lead time）與固定週報輸出。
  - 將 research quality gate 併入 promotion 前置檢核（引用完整性、可重現性、資料時效）。

## 3. Ops 與長期治理

### 3.1 三年運維固定檢核自動化（P1）
- 追蹤文件：`docs/operations/long-term-risk-register.md`
- 待辦：
  - 將月度/季度例行檢核（TTL、research data retention、SMART、Prometheus storage、OS updates）逐步自動化。
  - 將檢核結果統一附掛到月度可靠性審查包。

### 3.2 Runbook 空白 SOP 收斂（P2）
- 位置：`docs/runbooks/`
- 待辦：
  - 補齊仍需人工判斷的操作步驟，降低輪值人員依賴口耳傳承。
