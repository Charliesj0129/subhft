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
- `reports/shioaji_api_latency.json` / `.csv`
- `reports/e2e_latency.summary.json`
- `reports/*.heatmap.csv`

## 管理建議
1. 產物附上 `metadata`（commit hash、參數、時間）。
2. 長期保留資料放外部儲存，不留在 OS 盤。
3. 不要提交含憑證或敏感資訊的輸出檔。
