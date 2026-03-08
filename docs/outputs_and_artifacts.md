# Outputs and Artifacts

本專案主要輸出目錄與典型產物如下。

## 主要目錄
- `.wal/`：WAL jsonl（recorder 原始緩衝）
- `outputs/`：runtime 狀態與診斷輸出
- `reports/`：latency/profiling 報告
- `research/experiments/`：研究實驗 artifacts
- `data/`：本機資料與中間產物
- `.benchmarks/`：pytest-benchmark 產物

## 常見產物
- `outputs/contract_refresh_status.json`
- `outputs/feature_rollout_state.json`
- `outputs/decision_traces/*.jsonl`
- `outputs/roadmap_delivery/latest.json` / `latest.md`（TODO/ROADMAP 治理檢查）
- `outputs/roadmap_execution/summary/latest.json`（WS-G/WS-H 交付物執行摘要）
- `outputs/roadmap_execution/ws_g/latest_hotpath_matrix.json`
- `outputs/roadmap_execution/ws_h/latest_source_catalog.json`
- `outputs/release_converge/latest.json` / `latest.md`（發行收斂與深度清潔報告）
- `outputs/release_first_ops/latest.json` / `latest.md`（第一個可長期運營發布 gate 彙總決策）
- `outputs/release_converge/backups/root_reports_slim_*.json`（tracked root_reports 瘦身 manifest）
- `reports/shioaji_api_latency.json` / `.csv`
- `reports/e2e_latency.summary.json`
- `reports/*.heatmap.csv`

## MVP 發行補充

- `make release-converge-mvp` 會使用 `mvp_release + full gate`。
- `HFT_ALPHA_AUDIT_ENABLED=1 make release-first-ops-gate CHANGE_ID=...` 會聚合 `release_converge --skip-clean --skip-gate`、strict roadmap delivery、`release_channel_guard gate`、`reliability_review_pack`。
- `research/data` 只保留最小 smoke 樣本：
  - `research/data/processed/smoke/smoke_v1.npy`
  - `research/data/processed/smoke/smoke_v1.npy.meta.json`
- `research/knowledge/reports/root_reports/` 會套用白名單保留，刪除紀錄寫入 `outputs/release_converge/backups/`。

## 管理建議
1. 產物附上 `metadata`（commit hash、參數、時間）。
2. 長期保留資料放外部儲存，不留在 OS 盤。
3. 不要提交含憑證或敏感資訊的輸出檔。
