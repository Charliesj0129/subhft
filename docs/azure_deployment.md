# Deploying HFT Platform to Azure (HFT Optimized)

本指南包含兩條路徑：
1. **研究/成本優先**
2. **低延遲優先**

---

## Prerequisites
- Azure CLI
- 有效 Azure 訂閱

---

## Step 1: 建立 Resource Group + VM

### 1A) 成本優先（研究用）
- VM: `Standard_B2s`
- OS Disk 30GB

```bash
az login
az group create --name hft-rg --location japaneast

az vm create \
  --resource-group hft-rg \
  --name hft-vm \
  --image Ubuntu2204 \
  --size Standard_B2s \
  --admin-username hftadmin \
  --generate-ssh-keys \
  --public-ip-sku Standard \
  --storage-sku Standard_LRS
```

### 1B) 低延遲優先
- VM: `F4s_v2` 以上，啟用 **Accelerated Networking**
- 建議建立 **PPG**
- 資料盤：Premium SSD，掛載 `/mnt/data`

```bash
az ppg create -g hft-rg -n hft-ppg --type Standard

az vm create \
  --resource-group hft-rg \
  --name hft-lowlat-vm \
  --image Ubuntu2204 \
  --size Standard_F4s_v2 \
  --accelerated-networking true \
  --admin-username hftadmin \
  --generate-ssh-keys \
  --public-ip-sku Standard \
  --storage-sku Premium_LRS \
  --data-disk-sizes-gb 256 \
  --ppg hft-ppg
```

---

## Step 2: Auto Shutdown（成本控管）
```bash
az vm auto-shutdown \
  --resource-group hft-rg \
  --name hft-vm \
  --time 0600 \
  --email "your-email@domain"
```

---

## Step 3: 安裝 Docker + 啟動平台

SSH 進 VM：
```bash
ssh hftadmin@<Public-IP>
```

安裝 Docker：
```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
```

Clone 專案：
```bash
git clone <repo-url> hft_platform
cd hft_platform
```

低延遲 host tuning：
```bash
sudo ./ops.sh tune
sudo ./ops.sh hugepages
```

啟動（含 ClickHouse + Grafana）：
```bash
sudo ./ops.sh setup
```

若 ClickHouse 資料盤掛載在 `/mnt/data`：
```bash
export HFT_CH_DATA_ROOT=/mnt/data/clickhouse
sudo ./ops.sh setup
```

---

## Step 4: 驗證
- Metrics: `http://<VM-IP>:9090/metrics`
- Grafana: `http://<VM-IP>:3000`
- ClickHouse: `http://<VM-IP>:8123`

---

## Teardown
```bash
az group delete --name hft-rg --yes --no-wait
```
