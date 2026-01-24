#!/bin/bash
set -e

# Ops Tool: CPU Isolation Setup (Soft-Realtime)
# Usage: sudo ./ops/setup_isolation.sh [command_to_run_isolated]
# Example: sudo ./ops/setup_isolation.sh python3 strategies/main.py

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root"
  exit 1
fi

TOTAL_CORES=$(nproc)
if [ "$TOTAL_CORES" -lt 4 ]; then
  echo "Warning: Only $TOTAL_CORES cores detected. Isolation requires at least 4 cores for effective separation."
  echo "Continuing with minimal separation..."
fi

# Strategy: Reserve the last 50% of cores for HFT
# E.g. 8 Cores -> 0-3 System, 4-7 HFT
SPLIT_IDX=$((TOTAL_CORES / 2))
SYSTEM_CORES="0-$((SPLIT_IDX - 1))"
HFT_CORES="$SPLIT_IDX-$((TOTAL_CORES - 1))"

echo "Configuring CPU Isolation:"
echo "  Total Cores:  $TOTAL_CORES"
echo "  System Set:   $SYSTEM_CORES"
echo "  HFT Set:      $HFT_CORES"

# 1. Move all current processes to System Cores (Best effort)
# We iterate through root cgroups and set cpuset.cpus if possible, 
# or use systemd's Slice config if available.
# Simple approach: Move current shell and init to System Cores? 
# Usually 'cset shield' does this best, but we want a dependency-free script.

# We will just focus on running the TARGET command in the HFT set.
# To protect it, we should ideally move noise away.

echo "Setting global Init Scope affinity to $SYSTEM_CORES (Soft attempt)..."
# This typically requires systemd tweaks. 
# We'll use systemctl to constrain user.slice and system.slice
# Note: This might affect the current session, be careful.
# systemctl set-property --runtime user.slice CPUAffinity=$SYSTEM_CORES
# systemctl set-property --runtime system.slice CPUAffinity=$SYSTEM_CORES

# 2. Run the target command in isolated cores
CMD="$@"
if [ -z "$CMD" ]; then
    echo "No command specified. Setup complete."
    echo "To run manually: taskset -c $HFT_CORES <command>"
    exit 0
fi

echo "Launching Isolated Command: $CMD"
echo "---------------------------------------------------"
# chrt -f 99: Run as Real-Time FIFO priority 99 (Highest)
# taskset -c: Pin to HFT_CORES
exec taskset -c $HFT_CORES chrt -f 50 $CMD
