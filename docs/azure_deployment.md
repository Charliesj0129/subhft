
# Deploying HFT Platform to Azure (HFT Optimized)

本指南包含兩條路徑：
1. **學生/研究版**（成本優先）。
2. **HFT 低延遲版**（延遲/抖動優先，符合行動清單要求）。

## Prerequisites

1.  **Azure CLI**: [Install Azure CLI](https://docs.microsoft.com/en-us/cli/azure/install-azure-cli)
2.  **Azure Account**: Active subscription (e.g., Azure for Students).

## Step 1: Create Resource Group & VM

**區域**：日本東 / 東亞（香港）。

### 1A) 學生/研究版（成本優先）
* VM：`Standard_B2s`。
* 磁碟：OS 30GB + Standard SSD。

```bash
# 1. Login
az login

# 2. Create Resource Group in Japan East
az group create --name hft-rg --location japaneast

# 3. Create VM (B2s)
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

### 1B) HFT 低延遲版（延遲/抖動優先）
* VM：`F4s_v2` 做資料收集/回測；盤中建議 `Epngsv3/Dp_v5/Hp/Dpds_v5/LSv3` 視區域供應，務必支援 **Accelerated Networking**。
* PPG：若有多台（行情/交易/DB）請加入 **Proximity Placement Group**。
* NIC：開 **Accelerated Networking**、併後續調 **multiqueue + RSS/RPS**。
* 磁碟：OS >=64GB，資料碟 Premium/Ultra SSD 掛 `/mnt/data`，ClickHouse/WAL 只放資料碟。

```bash
# Create PPG (可選)
az ppg create -g hft-rg -n hft-ppg --type Standard

# Create VM with Accelerated Networking + larger data disk
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

## Step 2: Configure Cost Control (Auto-Shutdown) CRITICAL! 💰

To stay within the $100 student credit, configure the VM to shutdown automatically after market hours.
*   **Market Hours**: 09:00 - 13:30 (Taiwan Time is UTC+8).
*   **Shutdown Time**: 14:00 (Taiwan Time) = **06:00 UTC**.

```bash
# Enable Auto-Shutdown at 14:00 Taipei Time (06:00 UTC)
az vm auto-shutdown \
  --resource-group hft-rg \
  --name hft-vm \
  --time 0600 \
  --email "your-email@university.edu"
```

> [!NOTE]
> Running 9AM-2PM (5 hours/day) costs **~75% less** than 24/7.
> Estimated B2s Cost: **~$8.00 / Month**.

## Step 3: Configure Network Security

Open ports only for necessary services.

```bash
# Allow Grafana (3000)
az vm open-port --port 3000 --resource-group hft-rg --name hft-vm --priority 1010
# SSH (22) is enabled by default
```

## Step 4: Setup the VM

SSH into your new VM:

```bash
ssh hftadmin@<Public-IP-Address>
```

Install Docker & Docker Compose:

```bash
# Standard Docker Install
sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch="$(dpkg --print-architecture)" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  "$(. /etc/os-release && echo "$VERSION_CODENAME")" stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Setup User Group
sudo usermod -aG docker $USER
newgrp docker
```

### 4B) 低延遲主機調優
```bash
# CPU governor / irqbalance / sysctl
cd ~/hft_platform
sudo bash ops/host_tuning.sh

# 確認資料碟掛載 (範例)
sudo mkdir -p /mnt/data
sudo mount /dev/disk/azure/scsi1/lun0 /mnt/data
sudo chown $USER:$USER /mnt/data
```

## Step 5: Deploy the Platform

Clone your code and start the stack.

```bash
# 1. Clone
git clone https://github.com/your-user/hft_platform.git
cd hft_platform

# 2. (建議) 用資料碟
# HFT_CH_DATA_ROOT 讓 ClickHouse/WAL 固定在 /mnt/data
sudo HFT_CH_DATA_ROOT=/mnt/data/clickhouse ./ops.sh setup

# 3. 若需自行啟動服務
docker compose up -d
```

> GHCR 部署（CI/CD）：`.github/workflows/deploy-ghcr.yml` 會 Build & Push GHCR，SSH 到 VM 後 `docker compose pull && up`（使用 lowlatency/chdata overrides），避免 pip + nohup 模式。

## Step 6: Verification

1.  **Check Logs**: `docker compose logs -f hft_platform`
2.  **Grafana**: Visit `http://<VM-IP>:3000`

## Auto-Start (Optional)
To fully automate, set up an **Azure Automation Runbook** to start the VM at 08:50 (Taiwan Time). This is outside the scope of CLIs but can be done in the Azure Portal > Automation Accounts.

## Teardown
To delete everything:
```bash
az group delete --name hft-rg --yes --no-wait
```
