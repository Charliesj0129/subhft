# Azure 部署指南（VM / Container）

## 環境需求
- Python 3.12
- 選用：ClickHouse（若不使用，設定 `HFT_CLICKHOUSE_ENABLED=0` 或 `HFT_DISABLE_CLICKHOUSE=1`）
- 開放 TCP 9090（Prometheus metrics），ClickHouse 用戶端 8123（如有）

## VM 部署步驟（Ubuntu 例）
```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv git
git clone <repo-url> hft_platform && cd hft_platform
make install  # 建立 venv 並安裝依賴

# 準備環境變數（建議複製 .env.example）
cp .env.example .env
echo 'SHIOAJI_PERSON_ID=你的ID' >> .env
echo 'SHIOAJI_PASSWORD=你的密碼' >> .env
# 若不連 ClickHouse
echo 'HFT_CLICKHOUSE_ENABLED=0' >> .env
```

前台 smoke：
```bash
make run-sim   # 或 run-live（需 SHIOAJI_*）
```

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
ExecStart=/home/ubuntu/hft_platform/.venv/bin/python -m hft_platform run live --strategy demo --symbol 2330
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

## 容器部署要點（Container Apps / AKS）
- 建議 base image：`python:3.12-slim`
- 建置：
  ```dockerfile
  FROM python:3.12-slim
  WORKDIR /app
  COPY . /app
  RUN python -m pip install --upgrade pip && pip install -e .
  ENV PYTHONPATH=/app/src
  CMD ["python", "-m", "hft_platform", "run", "live", "--strategy", "demo", "--symbol", "2330"]
  ```
- 將 SHIOAJI_*、HFT_* 設為容器環境變數；若不用 ClickHouse，設定 `HFT_CLICKHOUSE_ENABLED=0`。
- 暴露 9090 供 metrics。

## 驗證
- Metrics: `curl http://<host>:9090/metrics` 應見 `feed_events_total`
- Log: `journalctl -u hft.service -f` 或容器日誌

## 常見問題
- 無 Shioaji 憑證：服務自動轉模擬並提示，可先 smoke 後再上 live。
- ClickHouse 未啟：預設 WAL-only；設定 `HFT_CLICKHOUSE_ENABLED=1` 並確保 8123 可連。
- 訂閱/合約：在 `config/symbols.yaml` 控制，總數勿超過 200。
