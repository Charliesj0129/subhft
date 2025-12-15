#!/bin/bash
set -e

# Load secrets from .env if present
if [ -f .env ]; then
    export $(cat .env | xargs)
fi

echo "Starting HFT Platform (Production Mode)..."
echo "PID: $$"

# Ensure directories exist
mkdir -p .wal data logs

# Run Main
# If inside container, uses python. If local, user might need to use .venv/bin/python or uv
if [ -f /.dockerenv ]; then
    exec python -m hft_platform.main
else
    # Local run with uv
    exec uv run python -m hft_platform.main
fi
