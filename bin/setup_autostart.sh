#!/bin/bash
set -e

# Configuration
RG="HFT-RG"
VM="hft-vm"
ACC="hft-auto-start"
LOC="japanwest" # Student Sub restriction: japaneast not allowed for automation.
TIME="08:30" # CST (Taipei Time) -> adjusted to UTC in schedule? 
# Azure Automation Schedule uses TimeZone. We can set "Asia/Taipei".

echo "Creating Automation Account: $ACC..."
az automation account create --name $ACC --resource-group $RG --location $LOC --sku Basic || echo "Account may already exist"

echo "Assigning System Identity..."
az resource update --resource-group $RG --name $ACC --resource-type "Microsoft.Automation/automationAccounts" --set identity.type="SystemAssigned"

echo "Fetching System Identity..."
PRINCIPAL_ID=$(az automation account show --name $ACC --resource-group $RG --query identity.principalId -o tsv)
echo "Identity ID: $PRINCIPAL_ID"

echo "Waiting 60s for Identity propagation..."
sleep 60

echo "Assigning 'Virtual Machine Contributor' role to Identity on Resource Group..."
# Get Subscription ID
SUB_ID=$(az account show --query id -o tsv)

# Retry loop for role assignment
for i in {1..5}; do
    az role assignment create --assignee-object-id $PRINCIPAL_ID --assignee-principal-type ServicePrincipal --role "Virtual Machine Contributor" --scope "/subscriptions/$SUB_ID/resourceGroups/$RG" && break
    echo "Role assignment failed. Retrying in 10s ($i/5)..."
    sleep 10
done

echo "Creating Runbook: Start-HFT-VM..."
# We need to upload a file or create content. CLI 'az automation runbook create' just makes the container.
az automation runbook create --automation-account-name $ACC --name "Start-HFT-VM" --resource-group $RG --type PowerShell

echo "Publishing Runbook Content..."
# We use a temporary file
cat <<EOF > /tmp/StartVM.ps1
param (
    [Parameter(Mandatory=\$false)]
    [object] \$WebhookData
)
# Connect using Managed Identity
Connect-AzAccount -Identity
Start-AzVM -ResourceGroupName "$RG" -Name "$VM"
EOF

az automation runbook replace-content --automation-account-name $ACC --name "Start-HFT-VM" --resource-group $RG --content @/tmp/StartVM.ps1
az automation runbook publish --automation-account-name $ACC --name "Start-HFT-VM" --resource-group $RG

echo "Creating Schedule: DailyStart at $TIME (Asia/Taipei)..."
az automation schedule create --automation-account-name $ACC --name "DailyStart" --resource-group $RG --frequency Day --interval 1 --start-time "$(date +%Y-%m-%d) $TIME:00" --time-zone "Asia/Taipei"

echo "Linking Runbook to Schedule..."
az automation job-schedule create --automation-account-name $ACC --resource-group $RG --runbook-name "Start-HFT-VM" --schedule-name "DailyStart"

echo "Done! $VM will start daily at $TIME Asia/Taipei."
