#!/bin/bash
set -euo pipefail

root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

changed=$(git -C "$root" diff --name-only HEAD 2>/dev/null || true)

has_list=false
has_yaml=false

if echo "$changed" | grep -q "config/symbols.list"; then
  has_list=true
fi
if echo "$changed" | grep -q "config/symbols.yaml"; then
  has_yaml=true
fi

if [ "$has_list" = true ] && [ "$has_yaml" = false ]; then
  echo "[Eval] symbols.list changed but symbols.yaml was not regenerated." >&2
fi
if [ "$has_list" = false ] && [ "$has_yaml" = true ]; then
  echo "[Eval] symbols.yaml changed without symbols.list updates." >&2
fi

if git -C "$root" ls-files --error-unmatch config/contracts.json >/dev/null 2>&1; then
  echo "[Eval] config/contracts.json is tracked. This should usually be generated and ignored." >&2
fi

if git -C "$root" ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo "[Eval] .env is tracked. This should not be committed." >&2
fi
