# HFT Platform 部署指南

本文件整理各種部署模式與環境設定，並指向更細節的 Runbook。

## 1. 部署模式總覽
| 模式 | 目的 | 指令 | 依賴 |
| --- | --- | --- | --- |
| 本機模擬 | 開發/驗證流程 | `make run-sim` | 不需憑證 |
| 本機實盤 | 接入券商 | `make run-prod` | SHIOAJI_* |
| Docker Compose | 開發 + ClickHouse | `docker compose up -d` | Docker |
| Ops 低延遲 | 低抖動部署 | `ops/docker/docker-compose.yml` | Linux host |
| Azure VM | 雲端 VM | `docs/azure_deployment.md` | Azure |

## 2. 部署核心原則
### 2.1 Infrastructure as Code (IaC)
建議使用 Terraform/Ansible/Kubernetes YAML 來描述環境，避免人工操作造成環境漂移。

### 2.2 Immutable Infrastructure 與容器化
使用 Docker 建置映像檔，一次建好後在開發/測試/生產環境一致執行。

### 2.3 Build / Release / Run 分離
- **Build**：`docker build -t hft-platform:latest .`
- **Release**：以 `.env` 或平台注入環境變數
- **Run**：`docker compose up -d`

### 2.4 藍綠部署 / 滾動更新
部署新版本時，保留舊版本流量，確認健康後再切換，確保可快速回滾。

## 3. 必要環境變數
**認證**
- `SHIOAJI_PERSON_ID`, `SHIOAJI_PASSWORD`：實盤登入。
- `SHIOAJI_API_KEY`, `SHIOAJI_SECRET_KEY`：API key 登入（優先於 person_id）。

**模式**
- `HFT_MODE=sim|live`：執行模式。
- `SYMBOLS_CONFIG=path`：symbols yaml 位置。

**監控**
- `HFT_PROM_PORT`：Prometheus port（預設 9090）。

**儲存**
- `HFT_CLICKHOUSE_ENABLED=0|1`
- `HFT_CLICKHOUSE_HOST`, `HFT_CLICKHOUSE_PORT`
- `HFT_DISABLE_CLICKHOUSE=1`：強制關閉 ClickHouse。

參考：`.env.example`

## 4. 本機開發 / 模擬
```bash
uv sync --dev
cp .env.example .env
make run-sim
```

## 5. 本機實盤
```bash
cp .env.example .env
export SHIOAJI_PERSON_ID="YOUR_ID"
export SHIOAJI_PASSWORD="YOUR_PWD"
make run-prod
```

## 6. Docker Compose（含 ClickHouse）
`docker-compose.yml` 提供開發與觀測堆疊：
```bash
export SHIOAJI_PERSON_ID=...
export SHIOAJI_PASSWORD=...
docker compose up -d
```

**服務**
- `hft-engine`：主程式（Prometheus `9090`）。
- `clickhouse`：`8123/9000`。
- `wal-loader`：WAL 回灌。
- `jupyter`：`8888`。

**資料與 WAL**
- `./.wal`、`./data`、`./config` 會掛載至容器。

## 7. Ops / 低延遲部署
`ops/docker/docker-compose.yml` 走 host network（Linux）以降低抖動：
```bash
cd ops/docker
# 準備 ops/docker/.env.prod（自行建立）
docker compose up -d
```

若在 Windows/Mac，請移除 `network_mode: host` 並使用 ports。

## 8. Azure VM
詳細步驟：
- `docs/deploy_azure.md`（簡化 VM/Container）
- `docs/azure_deployment.md`（低延遲版 + GHCR）

## 9. CI/CD
**CI**
- `.github/workflows/ci.yml`：lint/format/typecheck + tests。

**部署**
- `.github/workflows/deploy.yml`
- `.github/workflows/deploy-ghcr.yml`
> 需要設定 secrets 才能成功。

## 10. 監控與驗證
**Prometheus**
- `http://localhost:9090/metrics`

**Grafana**
- Ops 堆疊預設 `http://localhost:3000`

**健康檢查**
- Docker healthcheck 會確認主程序是否在運行。

## 11. 測試環境（Docker）
**System/Stress**
- `docker-compose.test.yml`
- `docker-compose.stress.yml`
- `scripts/run_system_tests.sh`
