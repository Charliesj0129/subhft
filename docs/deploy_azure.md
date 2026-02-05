# Azure 部署指南（VM / Container）

## 環境需求
- Python 3.12+
- Docker（若用 Compose）
- 開放 9090 (metrics), 8123 (ClickHouse), 3000 (Grafana)

---

## VM 部署（Ubuntu 範例）
```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv git

git clone <repo-url> hft_platform
cd hft_platform

# 安裝依賴
uv sync --dev

# 建立 env
cp .env.example .env
```

啟動模擬：
```bash
uv run hft run sim
```

若要 live：
```bash
export SHIOAJI_API_KEY=...
export SHIOAJI_SECRET_KEY=...
export HFT_MODE=live
uv run hft run live
```

---

## systemd 服務範例
`/etc/systemd/system/hft.service`
```
[Unit]
Description=HFT Platform
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/hft_platform
Environment="PYTHONPATH=/home/ubuntu/hft_platform/src"
EnvironmentFile=/home/ubuntu/hft_platform/.env
ExecStart=/home/ubuntu/hft_platform/.venv/bin/python -m hft_platform run live
Restart=always
User=ubuntu
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

啟用：
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hft.service
```

---

## Container 部署（Container Apps / AKS）
- 建議用 docker-compose 或自行 build image
- 環境變數透過平台設定 `SHIOAJI_*`, `HFT_*`
- 暴露 9090 供 metrics

---

## 驗證
```bash
curl http://<host>:9090/metrics
```

---

## 常見問題
- 缺 Shioaji 憑證：live 會自動降級 sim
- ClickHouse 未啟：設定 `HFT_CLICKHOUSE_ENABLED=0`
