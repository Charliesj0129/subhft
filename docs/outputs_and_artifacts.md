# Outputs and Artifacts

本專案產生的檔案會集中於以下資料夾：

## 主要目錄
- `.wal/`：WAL（raw jsonl），recorder 來源
- `data/`：ClickHouse 或外部資料
- `reports/`：latency report、py-spy SVG、CSV
- `results/`：實驗結果或分析輸出
- `research/`：研究腳本與暫存
- `.benchmarks/`：pytest-benchmark 產物

## 常見產物
- Shioaji API latency：`reports/shioaji_api_latency.json` / `.csv`
- E2E latency：`reports/e2e_latency.summary.json`
- Heatmap：`reports/*.heatmap.csv`

## 建議規範
- 不要提交含憑證的檔案
- 每次量測/實驗建議附 `metadata.json`（commit hash + params）
- 使用日期或任務名稱分資料夾（例如 `reports/20260203/`）
