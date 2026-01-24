#!/bin/bash
input=$(cat)
if echo "$input" | grep -q "config/symbols.yaml"; then
  echo "[Hook] NOTE: config/symbols.yaml is generated. Update config/symbols.list and run make sync-symbols." >&2
fi
if echo "$input" | grep -q "config/contracts.json"; then
  echo "[Hook] NOTE: config/contracts.json is generated. Do not commit unless asked." >&2
fi
printf "%s" "$input"
