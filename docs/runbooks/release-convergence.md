# Release Convergence Runbook

更新日期：2026-03-06

## 目標

將專案收斂至可發行狀態：完成深度盤點、清潔快取/暫存、執行核心 gate，並輸出可稽核報告。

## 角色與技能

- 角色（RACI 實務分工）
  - `planner`：盤點範圍、定義清潔邊界、確認 gate 順序。
  - `refactor-cleaner`：執行深度清潔（cache/artifact 清除）。
  - `code-reviewer`：檢查 gate 結果，阻擋 fail 狀態進入發行。
- 技能
  - `iterative-retrieval`：先檢索現況（`ls -a`、`tree -a`、`du -sh`、`git status`）。
  - `fix`：清潔後執行 lint/test，修復阻斷項。
  - `doc-updater`：將收斂流程與輸出更新到 docs。

## 指令

### 1) 只做盤點（不清潔）

```bash
make release-converge-scan
```

### 2) 深度清潔 + Gate（建議）

```bash
make release-converge
```

### 3) 含 Rust build artifact 清潔

```bash
make release-converge CLEAN_RUST=1
```

### 4) MVP 發行收斂（激進，含 tracked 瘦身 + full gate）

```bash
make release-converge-mvp
# 或：
make release-converge-mvp CLEAN_RUST=1
```

此模式會使用 `cleanup_profile=mvp_release`，預設行為：
- 清理高風險目錄（如 `.wal/`、`data/`、`outputs/`、`reports/`、`.state/`）
- 對 `research/knowledge/reports/root_reports/` 做 tracked 瘦身（保留最小白名單）
- 重建 `research/` 骨架並補種最小 smoke 樣本：
  - `research/data/processed/smoke/smoke_v1.npy`
  - `research/data/processed/smoke/smoke_v1.npy.meta.json`
- 以 `--gate-profile full` 作為發行 gate

### 5) 第一個可長期運營發布 gate（不清潔，fail-closed）

```bash
HFT_ALPHA_AUDIT_ENABLED=1 make release-first-ops-gate CHANGE_ID=CHG-YYYYMMDD-XX
# gate pass 後：
HFT_ALPHA_AUDIT_ENABLED=1 make release-first-ops-promote CHANGE_ID=CHG-YYYYMMDD-XX ACTOR=ops
```

此模式不做 cleanup，改以發布準入證據為主：
- 執行 `release_converge --skip-clean --skip-gate`，先落盤非破壞性盤點快照。
- strict `roadmap_delivery_executor` / `roadmap_delivery_guard`，`warn` 視為阻擋。
- release 關鍵 unit tests + `ruff` + `mypy`。
- `release_channel_guard gate`，確認 pre-sync manifest、canary、drift 證據。
- strict `reliability_review_pack`，確認月度運營證據可形成 go/no-go 決策。
- 前置要求 `HFT_ALPHA_AUDIT_ENABLED=1`。
- 若任何子步驟執行期間產生未預期 tracked 變更，整體直接 fail。

## 產物

- `outputs/release_converge/latest.json`
- `outputs/release_converge/latest.md`
- `outputs/release_converge/backups/root_reports_slim_*.json`（tracked 瘦身 manifest）
- `outputs/release_first_ops/latest.json`
- `outputs/release_first_ops/latest.md`

欄位重點：
- `before/after.sizes`：清潔前後容量快照。
- `cleanup_steps`：每個清潔步驟的 return code 與耗時。
- `gate_steps`：ROADMAP/TODO gate + 目標測試與 lint 結果。
- `result.overall`：`pass`/`fail`（`fail` 一律阻擋發行）。
- `outputs/release_first_ops/latest.*`：單一發布 go/no-go 決策，彙總 converge / channel / reliability 子報告。

## 判定規則

- 可發行：`result.overall=pass`。
- 阻擋：任何 cleanup/gate 步驟 return code 非 0。

## 注意事項

- `release-converge`（safe/extended）預設不刪 tracked source。
- `release-converge-mvp` 會刪除部分 tracked 歷史報告（依白名單保留），執行前應確認備份策略。
- `release-first-ops-gate` 不負責生成 pre-sync 產物；執行前仍需先跑 `make deploy-pre-sync-template CHANGE_ID=...`。
- `release-first-ops-gate` 會要求 `HFT_ALPHA_AUDIT_ENABLED=1`；若未設置，直接阻擋。
- `root_reports` 最小保留白名單：
  - `README.md`
  - `pyspy_hotspot_triage.md`
  - `e2e_latency.summary.json`
  - `latency_e2e.json`
