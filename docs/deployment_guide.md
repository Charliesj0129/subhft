# HFT Platform 部署指南

本文件整理本機、Docker、Ops 三種部署模式，並指向 Runbook。

---

## 1. 部署模式總覽
| 模式 | 目的 | 指令 | 依賴 |
| --- | --- | --- | --- |
| 本機模擬 | 開發/驗證流程 | `uv run hft run sim` | Python/uv |
| 本機 live | 接入券商 | `HFT_MODE=live uv run hft run live` | SHIOAJI_* |
| Docker Compose | 單機部署 + ClickHouse（預設） | `docker compose up -d --build` | Docker |
| Docker Swarm | 服務化部署（可選） | `docker stack deploy -c docker-stack.yml hft` | Docker + Swarm |
| Ops (Host Tuning) | 低抖動環境 | `sudo ./ops.sh setup` | Linux + sudo |

---

## 2. 本機部署

### 2.1 安裝
```bash
uv sync --dev
cp .env.example .env
```

### 2.2 產生 symbols
```bash
uv run hft config build --list config/symbols.list --output config/symbols.yaml
```

### 2.3 啟動
```bash
uv run hft run sim
```

---

## 3. Docker Compose 部署（預設）

```bash
docker compose up -d --build
```

### 3.1 服務清單
- `hft-engine`：主流程
- `wal-loader`：WAL 回灌 ClickHouse
- `clickhouse`：資料庫
- `redis`：state
- `prometheus` / `grafana` / `alertmanager`

### 3.2 常用命令
```bash
docker compose logs -f hft-engine

docker compose restart hft-engine

docker compose down
```

### 3.3 symbols.yaml 使用注意
docker-compose 預設 `SYMBOLS_CONFIG=config/base/symbols.yaml`。
若你要用 `config/symbols.yaml`：
```bash
# .env
SYMBOLS_CONFIG=config/symbols.yaml

# 然後
docker compose restart hft-engine
```

### 3.4 Docker Swarm（可選）
```bash
docker swarm init 2>/dev/null || true
docker build -t ${HFT_IMAGE:-hft-platform:latest} .
docker stack deploy -c docker-stack.yml hft
docker service logs -f hft_hft-engine
docker stack rm hft
```

---

## 4. Live 模式

```bash
export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
export HFT_MODE=live

# Optional CA
export SHIOAJI_PERSON_ID=...
export SHIOAJI_CA_PATH=/path/to/Sinopac.pfx
export SHIOAJI_CA_PASSWORD=...
export SHIOAJI_ACTIVATE_CA=1

uv run hft run live
```

---

## 5. Ops / 低延遲部署

> 適用 Linux Host。需要 root 權限。

```bash
sudo ./ops.sh setup
```

可選：
- `sudo ./ops.sh tune`（sysctl + cpu governor）
- `sudo ./ops.sh hugepages`
- `sudo ./ops.sh isolate '<command>'`

ClickHouse data path 可用環境變數覆蓋：
```bash
export HFT_CH_DATA_ROOT=/mnt/data/clickhouse
sudo ./ops.sh setup
```

---

## 6. CI / 部署更新
- CI: `.github/workflows/ci.yml`
- 部署：
  - 單機：`docker compose up -d --build`
  - 叢集：`docker stack deploy -c docker-stack.yml hft`

---

## 7. 驗證

### 7.1 Metrics
- `http://localhost:9090/metrics`

### 7.2 ClickHouse
```bash
docker exec clickhouse clickhouse-client --query \
  "SELECT count() FROM hft.market_data"
```

---

## 8. 相關文件
- `docs/runbooks.md`
- `docs/troubleshooting.md`
- `docs/observability_minimal.md`
