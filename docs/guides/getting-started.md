# HFT Platform 使用者指南（完整流程）

本文件提供「從零到可觀測可排錯」的完整路徑。

## 0. 前置需求

必要：
- Python 3.10+
- `uv`

選用：
- Docker + Docker Compose
- Rust toolchain（需要重編 `rust_core` 時）

檢查：
```bash
python --version
uv --version
```

## 1. 安裝與初始化
```bash
git clone <repo-url>
cd hft_platform
uv sync --dev
cp .env.example .env
```

> 若 `hft` 不在 PATH，使用 `uv run hft ...`。

## 2. symbols 流程（必要）

`config/symbols.list` 是唯一來源，`config/symbols.yaml` 由 CLI 生成。

```bash
uv run hft config preview
uv run hft config validate
uv run hft config build --list config/symbols.list --output config/symbols.yaml
```

若要同步券商合約快取並重建：
```bash
uv run hft config sync --list config/symbols.list --output config/symbols.yaml
uv run hft config contracts-status
```

## 3. 本機模擬啟動
```bash
uv run hft run sim
```

驗證：
```bash
curl -fsS http://localhost:9090/metrics | head
uv run hft feed status --port 9090
```

## 4. Docker Compose 部署（建議）

### 4.1 啟動順序（避免初期 DNS/依賴抖動）
```bash
docker compose up -d clickhouse redis
docker compose up -d --build hft-engine
# 可選觀測堆疊
docker compose up -d prometheus grafana alertmanager hft-monitor
```

### 4.1.1 單 runtime 原則（Shioaji）
- `hft-engine` 是唯一持有 broker session 的 runtime。
- `hft-base` 已改為 `maintenance` profile，預設不啟動，避免與 `hft-engine` 競爭同一組 session。
- 如需進入 maintenance 容器，請顯式啟用：
```bash
docker compose --profile maintenance up -d hft-base
```

### 4.2 檢查
```bash
docker compose ps
docker compose logs --tail=200 hft-engine
curl -fsS http://localhost:9090/metrics | head
```

服務端點：
- hft-engine metrics: 9090
- clickhouse: 8123/9000
- prometheus: 9091
- grafana: 3000
- alertmanager: 9093

注意：
- compose 若未設 `SHIOAJI_ACCOUNT` 會警告，但一般不影響 sim。
- `SYMBOLS_CONFIG` 預設是 `config/base/symbols.yaml`；若要改用自己生成版本，設定 `.env`：
```bash
SYMBOLS_CONFIG=config/symbols.yaml
```
並重啟：
```bash
docker compose restart hft-engine
```

## 5. Live 模式（顯式啟用）
```bash
export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
export HFT_MODE=live
uv run hft run live
```

CA 選用：
```bash
export SHIOAJI_PERSON_ID=...
export SHIOAJI_CA_PATH=/path/to/Sinopac.pfx
export SHIOAJI_CA_PASSWORD=...
export SHIOAJI_ACTIVATE_CA=1
```

若缺憑證，CLI 會自動降級為 `sim`。

## 6. 策略開發最小流程
```bash
uv run hft init --strategy-id my_strategy --symbol 2330
uv run hft strat test --symbol 2330
```

## 7. 回測流程（HftBacktest）
```bash
# JSONL -> NPZ
uv run hft backtest convert \
  --input data/sample_events.jsonl \
  --output data/sample_feed.npz \
  --scale 10000

# 執行回測
uv run hft backtest run \
  --data data/sample_feed.npz \
  --symbol 2330 \
  --report
```

策略 adapter：
```bash
uv run hft backtest run \
  --data data/sample_feed.npz \
  --strategy-module hft_platform.strategies.simple_mm \
  --strategy-class SimpleMarketMaker \
  --strategy-id demo \
  --symbol 2330
```

## 8. Recorder / ClickHouse 驗證
```bash
uv run hft recorder status

docker exec clickhouse clickhouse-client --query \
  "SELECT count() FROM hft.market_data"
```

## 9. 延遲量測
```bash
uv run python scripts/latency/shioaji_api_probe.py --mode sim --iters 30
uv run python scripts/latency/e2e_clickhouse_report.py --window-min 10
```

## 10. 問題排查入口
- [`docs/runbooks.md`](runbooks.md)
- [`docs/troubleshooting.md`](troubleshooting.md)
- [`docs/observability_minimal.md`](observability_minimal.md)
