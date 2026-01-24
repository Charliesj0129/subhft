#!/bin/bash
set -euo pipefail

root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
session_dir="$root/.claude-sessions"
mkdir -p "$session_dir"

ts=$(date -u "+%Y%m%dT%H%M%SZ")
file="$session_dir/session-${ts}-start.txt"

{
  echo "timestamp=$ts"
  echo "cwd=$(pwd)"
  echo "git_root=$root"
  git -C "$root" status -sb 2>/dev/null || true
  git -C "$root" log -1 --oneline 2>/dev/null || true
  if [ -x "$root/.venv/bin/python" ]; then
    "$root/.venv/bin/python" -m hft_platform config preview 2>/dev/null || true
  fi
} > "$file"

ln -sf "$(basename "$file")" "$session_dir/latest-start.txt"
