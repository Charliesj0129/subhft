#!/bin/bash
set -euo pipefail

root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
session_dir="$root/.claude-sessions"
mkdir -p "$session_dir"

ts=$(date -u "+%Y%m%dT%H%M%SZ")
file="$session_dir/session-${ts}-end.txt"

{
  echo "timestamp=$ts"
  git -C "$root" status -sb 2>/dev/null || true
  git -C "$root" diff --name-only 2>/dev/null || true
} > "$file"

ln -sf "$(basename "$file")" "$session_dir/latest-end.txt"
