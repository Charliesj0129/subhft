#!/usr/bin/env bash
# Minimal host tuning for low-latency HFT workloads on Ubuntu
# Run as root: sudo bash ops/host_tuning.sh
set -euo pipefail

echo "[tune] Applying sysctl network/queue settings..."
cat <<'SYSCTL' >/etc/sysctl.d/99-hft.conf
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.core.netdev_max_backlog = 250000
net.ipv4.tcp_rmem = 4096 87380 134217728
net.ipv4.tcp_wmem = 4096 65536 134217728
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_low_latency = 1
net.core.somaxconn = 4096
# Busy-poll only if your NIC/driver supports it; comment out otherwise
# net.core.busy_read = 50
# net.core.busy_poll = 50
SYSCTL
sysctl --system

echo "[tune] Setting CPU governor to performance..."
if command -v cpupower >/dev/null 2>&1; then
  cpupower frequency-set -g performance || true
else
  apt-get update && apt-get install -y linux-tools-common linux-tools-$(uname -r)
  cpupower frequency-set -g performance || true
fi

echo "[tune] Disabling irqbalance; pin IRQ 1:1 if NIC IRQ found (edit for your NIC)..."
systemctl stop irqbalance || true
systemctl disable irqbalance || true
if ls /proc/irq/*/smp_affinity_list >/dev/null 2>&1; then
  # Example: pin all IRQs to CPU0. Adjust as needed for your NIC queues.
  for i in /proc/irq/*/smp_affinity_list; do echo 0 > "$i" || true; done
fi

echo "[tune] Suggested grub flags (apply manually then reboot):"
echo "  GRUB_CMDLINE_LINUX=\"intel_pstate=disable intel_idle.max_cstate=0 processor.max_cstate=1 idle=poll isolcpus=nohz_full,rcu_nocbs=1\""
echo "  After editing /etc/default/grub: update-grub && reboot"

echo "[tune] Done. Verify with: sysctl -a | grep net.core.rmem_max"
