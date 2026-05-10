#!/bin/bash
# Wrapper to run the standardized research backtest runner
set -e

# Ensure we are in project root
if [ ! -d ".venv" ]; then
    echo "Error: .venv not found. Run from project root."
    exit 1
fi

echo ">> Starting Research Backtest Runner..."
.venv/bin/python -m research.backtest.hbt_runner "$@"
echo ">> Backtest finished."
