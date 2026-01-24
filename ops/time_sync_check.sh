#!/bin/bash
set -e

echo "Time Sync Check"
if command -v timedatectl >/dev/null 2>&1; then
  timedatectl status
else
  echo "timedatectl not available"
fi

echo ""
if command -v chronyc >/dev/null 2>&1; then
  chronyc tracking || true
else
  echo "chronyc not available"
fi

if command -v pmc >/dev/null 2>&1; then
  echo ""
  echo "PTP (pmc)" 
  pmc -u -b 0 "GET CURRENT_DATA_SET" || true
else
  echo "pmc not available"
fi
