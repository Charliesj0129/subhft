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
- **狀態**: 🔄 TODO（P2/P3 持續收斂中, 2026-03-02）
- **追蹤文檔**: `docs/architecture/shioaji-client-resilience-decoupling-plan.md`
- **剩餘技術債**:
  - `🔄 TODO [SHIOAJI-OPS-03]`: 將 Redis session owner preflight 擴充為週期性 lease refresh + stale owner cleanup（目前為 bootstrap warn-only）。
  - `🔄 TODO [SHIOAJI-DECOUPLE-05]`: 持續縮減 `shioaji_client.py`（目標 <1500 行）並移除剩餘 legacy shim。
  - `🔄 TODO [SHIOAJI-CANARY-01]`: 完成 production canary 指標驗收（first quote callback / reconnect 成功率）。

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

非同步冷路徑 (`HFT_RECORDER_MODE=wal_first`) 的 hardening 項目已落地，進入持續演練與營運監控階段：

- **狀態**: ✅ Implemented (2026-03-02)
- **追蹤編號**: `docs/architecture/cluster-evolution-backlog.md`
- **已完成**:
  - **[CE3-03]**: WAL Loader scale-out + shard claim（`FileClaimRegistry`）已實作，並有整合測試 `tests/integration/test_wal_loader_scale_out.py`。
  - **[CE3-04]**: Replay safety contract（ordering/dedup/manifest）已落地，並有 spec 測試 `tests/spec/test_replay_safety_contract.py`。
  - **[CE3-06]**: WAL SLO metrics + alerts + dashboard 已接線（`metrics.py`, `alerts/rules.yaml`, `dashboards/gateway_wal_slo.json`）。
  - **[CE3-07]**: Outage drills（CH down/slow、disk pressure、stale claim recovery）已落地，含測試與 runbook `docs/runbooks/wal-first-outage-drills.md`。
- **營運建議（非阻塞 TODO）**:
  - 每週執行一次 `verify-ce3`（或對應 pytest 套件）以驗證 replay 安全與災難演練路徑未回歸。

---

## 🔬 研究與分析工廠 (Research & Analytics)

### 1. Alpha 探索鷹架 (Alpha Scaffold)

- **位置**: `research/tools/alpha_scaffold.py`, `fetch_paper.py`, 及樣板檔案 (`_templates/impl.py.tmpl`)
- **狀態**: 🔄 TODO（迭代優化階段）
- **剩餘技術債**:
  - citation profile 仍是規則表，未來可升級為 parser + embeddings 的混合檢索。

### 2. 論文與引用清理 (Notes & Citations)

- **位置**: `research/knowledge/notes/` (如 `depth_slope_ref.md`)
- **狀態**: 🔄 TODO（批次修復中）
- **剩餘技術債**:
  - audit after backfill 仍有 `missing_any=93`（多數為缺 arXiv/作者/發布資訊來源），需補資料源或人工校對。

### 3. Pyspy / 效能探查結果 (Benchmarking)

- **位置**: `research/knowledge/reports/root_reports/*.svg` (Pyspy Flamegraphs)
- **狀態**: 🔄 TODO（目標化優化階段）
- **剩餘技術債**:
  - 依 triage 結果優先處理 `lob_engine.py` 熱點與 import/config warmup 開銷。

---

## 🔧 營運與其他瑣碎項目 (Ops & Observability)

- `🔄 TODO`: 針對 Feature Plane 導入 Dashboard / Alert wiring 以及 Canary 決策自動化機制 (`feature-engine-lob-research-unification-spec.md`)。
- 其他見於 Runbooks (`feature-plane-operations.md`, `incident-diagnostics.md`) 中的空白或未補齊的 SOP TODO 項目。
