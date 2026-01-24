#!/bin/bash
input=$(cat)
if echo "$input" | grep -q "make sync-symbols"; then
  if [ -z "${SHIOAJI_API_KEY:-}" ] && [ -z "${SHIOAJI_PERSON_ID:-}" ]; then
    echo "[Hook] NOTE: SHIOAJI credentials not detected in env. sync-symbols may fail." >&2
  fi
fi
if echo "$input" | grep -q "--mode live"; then
  if [ -z "${SHIOAJI_API_KEY:-}" ] && [ -z "${SHIOAJI_PERSON_ID:-}" ]; then
    echo "[Hook] NOTE: SHIOAJI credentials not detected in env. live mode may fail." >&2
  fi
fi
printf "%s" "$input"
