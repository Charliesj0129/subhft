# 技術債與 TODO 總覽 (Tech Debt & TODOs)

這份文件盤點了 codebase (`docs/`, `research/`) 中所有已知標記的 TODO 與潛在的架構技術債，協助未來的開發迭代與排程。

---

## 🏗 架構與核心路徑 (Architecture & Core)

### 1. Feature Plane Unification (特徵平面統一)

目前研究環境 (Research) 與實盤交易 (Runtime) 在「微結構特徵」的計算上有潛在的「特徵飄移風險」。現階段的目標是要引入統一的 **FeatureEngine (特徵平面)** 來解決此問題。

- **狀態**: 🔄 TODO (Planning Phase)
- **相關追蹤文檔**: `docs/architecture/feature-engine-lob-research-unification-spec.md`
- **細項 TODOs**:
  - `🔄 TODO`: 於 Runtime 中新增獨立的 `FeatureEngine` 組件 (介於 `LOBEngine` 與 `StrategyRunner` 之間)。
  - `🔄 TODO`: 針對回測模組開發 `HftBacktestAdapter` 的特徵優先模式 (`lob_feature` mode)。
  - `🔄 TODO`: 確保 Feature ABI/Versioning 在 Python Reference 與 Rust 實作之間的對齊機制。
  - `🔄 TODO`: 確認特徵傳輸的架構邊界 (Packed / Zero-copy 資料交換)。
  - `🔄 TODO`: 將目前屬於 Prototype 的特定 Rust Feature Kernels 推廣至 Production 狀態。

### 2. Shioaji Broker Adapter 解耦

- **現狀**: `feed_adapter/shioaji_client.py` 承載過多關注點，包含行情、連線、下單與帳戶快取。
- **狀態**: 🔄 TODO (Pending M2)
- **細項 TODOs**:
  - `🔄 TODO`: 拆分為獨立模組 (`session`, `contracts`, `quote`, `order`, `account`)，並實作Facade。

### 3. Gateway Hardening (Cluster Evolution Vector 2 - CE-M2)

雖然 Gateway 的核心功能已經上線 (`HFT_GATEWAY_ENABLED=1`)，但仍有幾項基礎設施加固 (Hardening Backlog) 等待補齊：

- **狀態**: 🔄 TODO (Hardening)
- **追蹤編號**: `docs/architecture/cluster-evolution-backlog.md`
- **細項 TODOs**:
  - **[CE2-07]**: 補齊 Gateway 的 Metrics / Alerts Dashboard 與 SLO 定義。
  - **[CE2-08]**: 多結點 (Multi-runner) 整合測試與 Gateway 斷線的 Chaos Test。
  - **[CE2-09]**: 實作 Active/Standby 的 Gateway 容災切換與 Leader Lease 控制機制。
  - **[CE2-11]**: 針對報價強制實施 Schema 鎖 (`quote_version=v1`) 與防護網。

### 4. WAL-First Path Hardening (Cluster Evolution Vector 3 - CE-M3)

雖然非同步冷路徑 (`HFT_RECORDER_MODE=wal_first`) 核心上線，但同屬防禦性加固的部分仍需推進：

- **狀態**: 🔄 TODO (Hardening)
- **追蹤編號**: `docs/architecture/cluster-evolution-backlog.md`
- **細項 TODOs**:
  - **[CE3-03]**: 橫向擴展 (Scale-out) WAL Loader Workers，並實作 Shard 指派策略。
  - **[CE3-04]**: 定義嚴謹的 Replay 安全合約 (包含訊息順序、去重、與 Manifest 維護)。
  - **[CE3-06]**: 補齊 WAL 的 SLO Metrics、Alerts 與 Dashboards。
  - **[CE3-07]**: 災難演練：實際演練 ClickHouse 停機、緩慢與 WAL 本機增長過載時的回復。

---

## 🔬 研究與分析工廠 (Research & Analytics)

### 1. Alpha 探索鷹架 (Alpha Scaffold)

- **位置**: `research/tools/alpha_scaffold.py`, `fetch_paper.py`, 及樣板檔案 (`_templates/impl.py.tmpl`)
- **狀態**: ✅ citation-aware + section-aware auto-fill 已落地 (2026-03-01)
- **已完成**:
  - `hypothesis/formula` 不再以 `TODO` placeholder 產生，改為 paper-aware 自動建議值。
  - `data_fields` 已接上 `feature registry (lob_shared_v1)` 進行候選映射。
  - 新增 citation-aware 映射（arXiv id profile）與 note section parser（讀 `Hypothesis`/`Formula`/`Relevant Features`）能力。
- **剩餘技術債**:
  - citation profile 仍是規則表，未來可升級為 parser + embeddings 的混合檢索。

### 2. 論文與引用清理 (Notes & Citations)

- **位置**: `research/knowledge/notes/` (如 `depth_slope_ref.md`)
- **狀態**: 🟡 工具化落地、批次修復進行中 (2026-03-01)
- **已完成**:
  - `depth_slope_ref.md` 已補 concrete citation，並在 `paper_index.json` 補齊 `arxiv_id` 對應。
  - 新增 `python -m research audit-note-citations` / `backfill-note-citations` 批次工具。
  - 已執行一次 backfill：`touched_notes=119`、`touched_index_rows=26`（詳見 `outputs/research_maintenance/`）。
- **剩餘技術債**:
  - audit after backfill 仍有 `missing_any=93`（多數為缺 arXiv/作者/發布資訊來源），需補資料源或人工校對。

### 3. Pyspy / 效能探查結果 (Benchmarking)

- **位置**: `research/knowledge/reports/root_reports/*.svg` (Pyspy Flamegraphs)
- **狀態**: ✅ 已建立 triage 基線，進入目標化優化階段
- **已完成**:
  - 新增 `python -m research triage-pyspy` 解析 SVG flamegraphs 並產出熱點排行。
  - 已產生報告：`outputs/research_maintenance/pyspy_triage.json` 與 `research/knowledge/reports/root_reports/pyspy_hotspot_triage.md`。
- **剩餘技術債**:
  - 依 triage 結果優先處理 `lob_engine.py` 熱點與 import/config warmup 開銷。

---

## 🔧 營運與其他瑣碎項目 (Ops & Observability)

- `🔄 TODO`: 針對 Feature Plane 導入 Dashboard / Alert wiring 以及 Canary 決策自動化機制 (`feature-engine-lob-research-unification-spec.md`)。
- 其他見於 Runbooks (`feature-plane-operations.md`, `incident-diagnostics.md`) 中的空白或未補齊的 SOP TODO 項目。
