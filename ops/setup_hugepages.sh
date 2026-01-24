#!/bin/bash
set -e
# Ops Tool: Enable Hugepages
# Goal: Reduce TLB misses for large Numba arrays / ClickHouse

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root"
  exit 1
fi

# 1024 * 2MB = 2GB Hugepages
NR_PAGES=${1:-1024}

echo "Setting Hugepages to $NR_PAGES..."
echo $NR_PAGES > /proc/sys/vm/nr_hugepages

# Verify
ACTUAL=$(cat /proc/sys/vm/nr_hugepages)
echo "Hugepages set: $ACTUAL"

if [ "$ACTUAL" -lt "$NR_PAGES" ]; then
    echo "Warning: Only allocated $ACTUAL pages. Memory might be fragmented or full."
fi

# Ensure /dev/hugepages is mounted
if ! mount | grep -q "/dev/hugepages"; then
    echo "Mounting /dev/hugepages..."
    mount -t hugetlbfs none /dev/hugepages
fi
