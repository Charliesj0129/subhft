# HFT Platform

高效能事件驅動交易平台（Shioaji + ClickHouse + Prometheus/Grafana + HftBacktest）。

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

## Docker Compose（預設單機部署）
```bash
# 建議先起資料服務
docker compose up -d clickhouse redis

# 再起主服務與觀測
docker compose up -d --build hft-engine prometheus grafana alertmanager hft-monitor

# 看主流程日誌
docker compose logs -f hft-engine
```

常用端點：
- Metrics: http://localhost:9090/metrics
- Prometheus UI: http://localhost:9091
- Grafana: http://localhost:3000
- Alertmanager: http://localhost:9093
- ClickHouse HTTP: http://localhost:8123

注意：
- 若未設定 `SHIOAJI_ACCOUNT`，compose 會顯示 warning；通常不影響 sim。
- Grafana 預設帳號 `admin`，密碼由 `GRAFANA_ADMIN_PASSWORD`（預設 `changeme`）。

停止：
```bash
docker compose down
```

## Live 模式（需顯式開啟）
```bash
export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
export HFT_MODE=live

# 選用：CA 簽章
export SHIOAJI_PERSON_ID=...
export SHIOAJI_CA_PATH=/path/to/Sinopac.pfx
export SHIOAJI_CA_PASSWORD=...
export SHIOAJI_ACTIVATE_CA=1

uv run hft run live
```

若缺 `SHIOAJI_API_KEY/SHIOAJI_SECRET_KEY`，CLI 會自動降級為 `sim`。

## 常用命令
```bash
# 設定與 symbols
uv run hft config preview
uv run hft config validate
uv run hft config contracts-status

# 回測
uv run hft backtest convert --input data/sample.jsonl --output data/sample.npz
uv run hft backtest run --data data/sample.npz --symbol 2330

# Recorder 狀態
uv run hft recorder status
```

## 測試與品質
```bash
uv run ruff check src/ tests/
uv run pytest
```

## 文件入口
- [docs/getting_started.md](docs/getting_started.md)
- [docs/quickstart.md](docs/quickstart.md)
- [docs/cli_reference.md](docs/cli_reference.md)
- [docs/config_reference.md](docs/config_reference.md)
- [docs/deployment_guide.md](docs/deployment_guide.md)
- [docs/runbooks.md](docs/runbooks.md)
- [docs/troubleshooting.md](docs/troubleshooting.md)

## 專案地圖（Top Level）
- `src/hft_platform/`: runtime 核心
- `config/`: 設定與 symbols
- `docs/`: 文件
- `scripts/`: 延遲量測與工具腳本
- `research/`: 研究工廠（alpha pipeline）
- `rust_core/`: Rust 擴展
- `tests/`: unit / integration / benchmark
