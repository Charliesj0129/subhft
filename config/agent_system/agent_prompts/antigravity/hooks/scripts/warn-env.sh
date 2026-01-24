#!/bin/bash
input=$(cat)
if echo "$input" | grep -q "\.env"; then
  echo "[Hook] NOTE: .env files may contain secrets. Do not commit." >&2
fi
printf "%s" "$input"
