#!/bin/bash
set -e
# Ops Tool: Install Real-Time Kernel (Debian/Ubuntu Bare Metal)
# WARNING: This script modifies GRUB and Kernel. Do not run on WSL2 or Managed VMs.

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root"
  exit 1
fi

echo "Installing RT Kernel..."
# Detect OS
source /etc/os-release
if [[ "$ID" == "debian" ]]; then
    # Ensure backports is enabled? (Skipped for brevity, assume configured)
    apt-get update
    apt-get install -y linux-image-rt-amd64 linux-headers-rt-amd64
elif [[ "$ID" == "ubuntu" ]]; then
    echo "Ubuntu detected. Ensure you have Ubuntu Pro or Realtime repo enabled."
    apt-get install -y linux-image-realtime linux-headers-realtime
else
    echo "Unsupported OS for auto-install: $ID"
    exit 1
fi

echo "Configuring GRUB for Low Latency..."
# Best Practice: Isolate Cores 2-N, Tickless Mode on Isolated Cores
# Adjust valid cpus based on `nproc`
ISOL_CPUS="2-$(($(nproc)-1))"

# Backup GRUB
cp /etc/default/grub /etc/default/grub.bak.$(date +%s)

# Append parameters
# isolcpus: Isolate from scheduler
# nohz_full: Tickless on these cores (if single task running)
# rcu_nocbs: Offload RCU callbacks from these cores
PARAMS="isolcpus=$ISOL_CPUS nohz_full=$ISOL_CPUS rcu_nocbs=$ISOL_CPUS skew_tick=1"

sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="/GRUB_CMDLINE_LINUX_DEFAULT="'"$PARAMS"' /' /etc/default/grub

echo "Updating GRUB..."
update-grub

echo "Done. Please REBOOT to activate RT Kernel."
