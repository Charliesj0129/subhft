#!/bin/bash
# Ops Tool: PTP Setup (Bare Metal)
# Usage: sudo ./setup_ptp.sh [interface_name]

IFACE=${1:-eth0}

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root"
  exit 1
fi

echo "Starting PTP on interface: $IFACE"

# Check for hardware timestamping support
ethtool -T $IFACE | grep "SOF_TIMESTAMPING_TX_HARDWARE" > /dev/null
if [ $? -ne 0 ]; then
    echo "WARNING: Interface $IFACE does not suggest HW Timestamping support."
fi

# 1. Start ptp4l (Slave Mode, align PHC to GM)
# -s: slaveOnly
# -i: interface
# -f: config
echo "Starting ptp4l..."
ptp4l -i $IFACE -f ./ptp4l.conf -s -m &
PTP_PID=$!

# 2. Start phc2sys (Sync System Clock to PHC/NIC Clock)
# -s: source (NIC)
# -c: clock (CLOCK_REALTIME)
# -w: wait for ptp4l to sync
echo "Starting phc2sys..."
phc2sys -s $IFACE -c CLOCK_REALTIME -w -m &
PHC_PID=$!

echo "PTP Services Running (PIDs: $PTP_PID, $PHC_PID). Press Ctrl+C to stop."
wait $PTP_PID $PHC_PID
