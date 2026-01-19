#!/bin/bash
set -e

# Configuration (override via env: RG_NAME, LOCATION, VM_NAME, VM_SIZE, DATA_DISK_GB, ACC_NET, PPG_NAME, OS_DISK_GB)
RG_NAME="${RG_NAME:-hft_resource_group}"
LOCATION="${LOCATION:-eastasia}"
VM_NAME="${VM_NAME:-hft-vm}"
# Default to HFT-friendly Fsv2; override to B2s for cost saving
VM_SIZE="${VM_SIZE:-Standard_F4s_v2}"
DATA_DISK_GB="${DATA_DISK_GB:-256}"
OS_DISK_GB="${OS_DISK_GB:-64}"
ACC_NET="${ACC_NET:-true}"   # Accelerated Networking
PPG_NAME="${PPG_NAME:-}"      # Set to enable Proximity Placement Group
ADMIN_USER="${ADMIN_USER:-azureuser}"

echo "Deploying to Azure ($LOCATION)..."

# 1. Create Resource Group
echo "Checking Resource Group $RG_NAME..."
if [ "$(az group exists --name $RG_NAME)" = "false" ]; then
    az group create --name $RG_NAME --location $LOCATION
    echo "Resource Group $RG_NAME created."
else
    echo "Resource Group $RG_NAME already exists."
fi

# 2. Create VM
# Check if VM exists
echo "Checking VM $VM_NAME..."
VM_EXISTS=$(az vm show -g $RG_NAME -n $VM_NAME --query "id" -o tsv 2>/dev/null || echo "")

if [ -z "$VM_EXISTS" ]; then
    echo "Creating VM $VM_NAME ($VM_SIZE)..."

    # Optional PPG for colocation
    PPG_ARG=""
    if [ -n "$PPG_NAME" ]; then
        if [ "$(az ppg list -g $RG_NAME --query \"[?name=='$PPG_NAME'] | length(@)\" -o tsv)" = "0" ]; then
            az ppg create -g $RG_NAME -n $PPG_NAME --type Standard
        fi
        PPG_ARG="--ppg $PPG_NAME"
    fi

    # Optional data disk
    DATA_DISK_ARG=""
    if [ -n "$DATA_DISK_GB" ]; then
        DATA_DISK_ARG="--data-disk-sizes-gb $DATA_DISK_GB"
    fi

    ACC_ARG=""
    if [ "$ACC_NET" = "true" ]; then
        ACC_ARG="--accelerated-networking true"
    fi

    az vm create \
        --resource-group $RG_NAME \
        --name $VM_NAME \
        --image Ubuntu2204 \
        --size $VM_SIZE \
        --admin-username $ADMIN_USER \
        --ssh-key-value $HOME/.ssh/hft_deploy_key.pub \
        --public-ip-sku Standard \
        --os-disk-size-gb $OS_DISK_GB \
        $DATA_DISK_ARG \
        $ACC_ARG \
        $PPG_ARG
else
    echo "VM $VM_NAME already exists."
fi

# 3. Open Ports
echo "Ensuring SSH port is open..."
az vm open-port --resource-group $RG_NAME --name $VM_NAME --port 22 --priority 100 >/dev/null

# 4. Get Public IP
IP=$(az vm show -d -g $RG_NAME -n $VM_NAME --query publicIps -o tsv)
echo "VM Public IP: $IP"

# 5. Prepare Payload
echo "Preparing configuration..."
# Create local .env for transfer (using provided env vars)
# WARNING: This file contains secrets.
cat <<EOT > .env
SHIOAJI_API_KEY=${SHIOAJI_API_KEY}
SHIOAJI_SECRET_KEY=${SHIOAJI_SECRET_KEY}
SHIOAJI_PERSON_ID=
SHIOAJI_PASSWORD=
SHIOAJI_ACCOUNT=
HFT_CLICKHOUSE_ENABLED=1
EOT

echo "Packaging project..."
# Remove old archive if exists
rm -f project.tar.gz
# Use set +e to ignore tar exit code 1 (file changed as read)
set +e
tar -czf project.tar.gz \
    --exclude='.venv' \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='target' \
    --exclude='data' \
    --exclude='.wal' \
    .
TAR_EXIT=$?
set -e
if [ $TAR_EXIT -ne 0 ] && [ $TAR_EXIT -ne 1 ]; then
    echo "Tar failed with code $TAR_EXIT"
    exit $TAR_EXIT
fi

# Remove local secret file immediately
rm .env

# 6. Transfer and Execute
echo "Waiting for SSH to be ready..."
SSH_OPTS="-i $HOME/.ssh/hft_deploy_key -o StrictHostKeyChecking=no -o ConnectTimeout=5"
# Simple loop to wait for SSH
count=0
while ! ssh $SSH_OPTS $ADMIN_USER@$IP "echo ready" &>/dev/null; do
    sleep 5
    count=$((count+1))
    if [ $count -gt 20 ]; then echo "SSH Timeout"; exit 1; fi
    echo -n "."
done
echo " SSH Ready."

echo "Uploading project archive..."
scp $SSH_OPTS project.tar.gz $ADMIN_USER@$IP:~/

echo "Executing setup on VM..."
ssh $SSH_OPTS $ADMIN_USER@$IP << EOF
    mkdir -p ~/hft_platform
    tar -xzf ~/project.tar.gz -C ~/hft_platform
    rm ~/project.tar.gz
    chmod +x ~/hft_platform/ops/setup_vm.sh
    cd ~/hft_platform
    ./ops/setup_vm.sh
EOF

rm project.tar.gz
echo "Deployment Finished. Services should be running on $IP."
