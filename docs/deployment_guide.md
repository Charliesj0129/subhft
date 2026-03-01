# HFT Platform 部署指南

本文件覆蓋本機、Docker Compose、Swarm 與 Live 啟動重點。

## 1. 部署模式總覽

| 模式 | 目的 | 指令 |
| --- | --- | --- |
| 本機模擬 | 開發與功能驗證 | `uv run hft run sim` |
| 本機 live | 串接券商 | `HFT_MODE=live uv run hft run live` |
| Docker Compose | 單機完整堆疊 | `docker compose up -d ...` |
| Docker Swarm | 可選服務化部署 | `docker stack deploy ...` |

## 2. 本機部署

```bash
uv sync --dev
cp .env.example .env
uv run hft config build --list config/symbols.list --output config/symbols.yaml
uv run hft run sim
```

## 3. Docker Compose（預設）

### 3.1 建議啟動順序
```bash
# 先起資料與快取
docker compose up -d clickhouse redis

# 再起主流程
docker compose up -d --build hft-engine

# 最後起觀測（可選）
docker compose up -d prometheus grafana alertmanager hft-monitor
```

### 3.2 驗證
```bash
docker compose ps
docker compose logs --tail=200 hft-engine
curl -fsS http://localhost:9090/metrics | head
```

### 3.3 常用命令
```bash
docker compose logs -f hft-engine
docker compose restart hft-engine
docker compose down
```

### 3.4 `SYMBOLS_CONFIG` 注意
compose 預設 `SYMBOLS_CONFIG=config/base/symbols.yaml`。若要改用生成版本：
```bash
# .env
SYMBOLS_CONFIG=config/symbols.yaml

docker compose restart hft-engine
```

## 4. Live 模式

```bash
export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
export HFT_MODE=live
uv run hft run live
```

CA（選用）：
```bash
export SHIOAJI_PERSON_ID=...
export SHIOAJI_CA_PATH=/path/to/Sinopac.pfx
export SHIOAJI_CA_PASSWORD=...
export SHIOAJI_ACTIVATE_CA=1
```

## 5. Swarm（可選）
```bash
docker swarm init 2>/dev/null || true
docker build -t ${HFT_IMAGE:-hft-platform:latest} .
docker stack deploy -c docker-stack.yml hft
docker service logs -f hft_hft-engine
```

## 6. Ops / Host Tuning（低延遲）
```bash
sudo ./ops.sh tune
sudo ./ops.sh hugepages
sudo ./ops.sh setup
```

## 7. 版本更新流程
```bash
git pull
docker compose up -d --build hft-engine
```

## 8. 相關文件
- `docs/runbooks.md`
- `docs/troubleshooting.md`
- `docs/hft_low_latency_runbook.md`

## 9. Azure Deployment ☁️
If you plan to run the stack on Azure, you can follow these standardized steps:

### Provisioning (Azure CLI)
```bash
az login
az group create --name hft-rg --location japaneast
az vm create \
  --resource-group hft-rg \
  --name hft-vm \
  --image Ubuntu2204 \
  --size Standard_B2s \ # Or Standard_F4s_v2 for lower latency
  --admin-username hftadmin \
  --generate-ssh-keys \
  --public-ip-sku Standard \
  --storage-sku Standard_LRS
```

### Setup Sequence
Once SSH'd into the VM, run the standard Docker install script, then clone the repository.
Copy `.env.example` -> `.env` and set critical passwords: `CLICKHOUSE_PASSWORD`, `REDIS_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`.
Start the stack sequentially as noted in step 3 to avoid DB provisioning timeouts in constrained environments.

**Cost Saving Tip**: Set Auto Shutdown on the VM and only run it during market hours. Monitor WAL/Clickhouse storage carefully as market data grows extensively.
