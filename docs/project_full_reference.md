# HFT Platform — Full Project Reference

本文件是「全域索引版」說明，對齊 2026-03-01 代碼狀態。

## 1) 專案定位

HFT Platform 是事件驅動交易系統，核心路徑：

```text
Feed -> Normalize -> LOB -> EventBus -> Strategy -> Risk -> Order -> Broker
                                   \-> Recorder (WAL/ClickHouse)
```

並包含：
- Feature Plane（profile/rollout/compat）
- Gateway（HA、去重、曝險控制）
- Alpha 研究治理（validate/promote/canary）

## 2) Repo Top-Level

| Path | 作用 |
| --- | --- |
| `.agent/` | Agent 規則/流程/技能 |
| `.github/` | CI workflows |
| `config/` | 基礎與環境設定、symbols、監控 |
| `docs/` | 使用/運維/架構文件 |
| `scripts/` | latency、ops、diagnostics 腳本 |
| `src/hft_platform/` | 主程式碼 |
| `research/` | 研究工廠與 alpha pipeline |
| `rust_core/` | Rust PyO3 擴展 |
| `rust/` | Rust strategy crate |
| `tests/` | unit/integration/benchmark |
| `outputs/` | runtime 狀態、trace、治理狀態 |
| `.wal/` | WAL 檔案 |

## 3) Runtime 模組地圖（`src/hft_platform/`）

### 3.1 入口與組裝
- `cli.py`：CLI 指令入口
- `main.py`：啟動 runtime 與 metrics server
- `services/bootstrap.py`：服務初始化與 queue 容量配置
- `services/system.py`：系統生命週期協調

### 3.2 Feed / Market Data
- `feed_adapter/shioaji_client.py`：登入、訂閱、重連、quote watchdog
- `feed_adapter/normalizer.py`：行情 payload 正規化
- `feed_adapter/lob_engine.py`：LOB 更新與統計
- `services/market_data.py`：市場資料流程主協調

### 3.3 Event 與 Bus
- `engine/event_bus.py`
- `events.py`
- `contracts/`（strategy/execution contract）

### 3.4 Strategy / Feature
- `strategy/base.py`, `strategy/runner.py`, `strategy/registry.py`
- `strategies/`（含 alpha strategy 實作）
- `feature/engine.py`, `feature/profile.py`, `feature/rollout.py`, `feature/compat.py`

### 3.5 Risk / Gateway / Order / Execution
- `risk/engine.py`, `risk/validators.py`, `risk/storm_guard.py`
- `gateway/service.py`, `gateway/leader_lease.py`, `gateway/dedup.py`, `gateway/exposure.py`
- `order/adapter.py`, `order/deadletter.py`
- `execution/positions.py` 等

### 3.6 Recorder / Observability / Diagnostics
- `recorder/writer.py`, `recorder/worker.py`, `recorder/wal.py`, `recorder/loader.py`
- `observability/metrics.py`, `observability/latency.py`
- `diagnostics/trace.py`, `diagnostics/replay.py`

### 3.7 Alpha 治理
- `alpha/validation.py`, `alpha/promotion.py`, `alpha/canary.py`, `alpha/experiments.py`, `alpha/pool.py`

## 4) 設定系統（`config/`）

- `config/base/main.yaml`
- `config/env/sim|live/main.yaml`
- `config/env/dev|staging|prod/main.yaml`
- `config/symbols.list`（來源）
- `config/symbols.yaml`（產物）
- `config/contracts.json`（合約快取）
- `config/feature_profiles.yaml`
- `config/monitoring/`（prometheus/grafana/alerts）

優先序：Base -> mode/env overlay -> settings.py -> env -> CLI。

## 5) CLI 總覽

主指令群：
- `hft run`, `init`, `check`, `wizard`, `feed`, `diag`
- `hft config`（`resolve/build/preview/validate/sync/contracts-status`）
- `hft feature`（`profiles/validate/preflight/rollout-*`）
- `hft strat test`
- `hft backtest convert/run`
- `hft recorder status`
- `hft alpha`（研究治理）

## 6) Docker / 部署

- 主文件：`docker-compose.yml`, `docker-stack.yml`
- 服務：`hft-engine`, `clickhouse`, `redis`, `wal-loader`, `prometheus`, `grafana`, `alertmanager`, `hft-monitor`

建議啟動順序：
```bash
docker compose up -d clickhouse redis
docker compose up -d --build hft-engine
docker compose up -d prometheus grafana alertmanager hft-monitor
```

## 7) 觀測與健康檢查

- Metrics: `http://localhost:9090/metrics`
- Prometheus: `http://localhost:9091`
- Grafana: `http://localhost:3000`

最小檢查：
```bash
curl -fsS http://localhost:9090/metrics | head
uv run hft recorder status
docker compose ps
```

## 8) 測試與 CI

本機常用：
```bash
uv run ruff check src/ tests/
uv run pytest
```

CI 主要階段：
- lint / security / type check
- Rust build & lint
- tests & coverage
- benchmark
- integration tests

## 9) Makefile 快捷

```bash
make dev
make test
make start
make logs
make recorder-status
make research
```

## 10) Research Factory（`research/`）

研究流程入口：
- `python -m research ...`
- `make research ...`

常用：
```bash
make research-scaffold ALPHA=<alpha_id>
make research-fetch-paper ARXIV=<id>
make research-search-papers QUERY="order flow imbalance"
```

## 11) 產物與資料

- `.wal/`：WAL
- `outputs/`：trace、rollout state、contract refresh status
- `reports/`：latency/benchmark 報告
- `research/experiments/`：研究實驗結果

## 12) 參考入口
- `docs/getting_started.md`
- `docs/cli_reference.md`
- `docs/config_reference.md`
- `docs/deployment_guide.md`
- `docs/runbooks.md`
- `docs/troubleshooting.md`
