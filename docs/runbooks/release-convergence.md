# Release Convergence Runbook

更新日期：2026-03-05

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

## 產物

- `outputs/release_converge/latest.json`
- `outputs/release_converge/latest.md`

欄位重點：
- `before/after.sizes`：清潔前後容量快照。
- `cleanup_steps`：每個清潔步驟的 return code 與耗時。
- `gate_steps`：ROADMAP/TODO gate + 目標測試與 lint 結果。
- `result.overall`：`pass`/`fail`（`fail` 一律阻擋發行）。

## 判定規則

- 可發行：`result.overall=pass`。
- 阻擋：任何 cleanup/gate 步驟 return code 非 0。

## 注意事項

- 深度清潔預設僅刪除 cache/暫存/coverage/prof 檔案，不刪除 tracked source。
- 大型 runtime 資料（如 `.wal`、`data/`）不在預設刪除範圍，避免誤刪營運資料。
