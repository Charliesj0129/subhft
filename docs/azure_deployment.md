
# Deploying HFT Platform to Azure (Student Optimized)

This guide walks you through deploying the HFT Platform to an **Azure Virtual Machine** using Docker, with a focus on **Cost Optimization** for students (Budget: <$15/mo).

## Prerequisites

1.  **Azure CLI**: [Install Azure CLI](https://docs.microsoft.com/en-us/cli/azure/install-azure-cli)
2.  **Azure Account**: Active subscription (e.g., Azure for Students).

## Step 1: Create Resource Group & VM

**Region Strategy**: We use **Japan East** or **East Asia (Hong Kong)** for low latency to Taiwan.
**VM Strategy**:
*   **Recording/Research**: `Standard_B2s` (2 vCPU, 4GB RAM) - ~$30/mo (before shutdown savings).
*   **Live Trading**: `Standard_F4s_v2` (Compute Optimized) - ~$170/mo.

```bash
# 1. Login
az login

# 2. Create Resource Group in Japan East
az group create --name hft-rg --location japaneast

# 3. Create VM (Student Budget Choice: B2s)
# Uses Standard SSD (LRS) to save cost vs Premium SSD
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

## Step 5: Deploy the Platform

Clone your code and start the stack.

```bash
# 1. Clone (or simple copy if private)
git clone https://github.com/your-user/hft_platform.git
cd hft_platform/deployment

# 2. Create Env File
cat <<EOF > .env.prod
SHIOAJI_PERSON_ID=YOUR_ID
SHIOAJI_PASSWORD=YOUR_PASS
HFT_MODE=live
CLICKHOUSE_HOST=clickhouse
EOF

# 3. Start Stack
docker compose up -d --build
```

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
