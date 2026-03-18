#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REMOTE_USER="${MONITOR_REMOTE_USER:-charl}"
REMOTE_HOST="${MONITOR_REMOTE_HOST:-100.91.176.126}"
TUNNEL_PORT="${MONITOR_TUNNEL_PORT:-18123}"
REMOTE_CH_PORT="${MONITOR_REMOTE_CH_PORT:-8123}"
WATCHLIST_PATH="${MONITOR_WATCHLIST:-config/watchlist.yaml}"
SYMBOLS_PATH="${MONITOR_SYMBOLS_PATH:-config/symbols.yaml}"
SOURCE="${HFT_MONITOR_SOURCE:-${MONITOR_SOURCE:-redis}}"
CH_USER="${HFT_CLICKHOUSE_USER:-${MONITOR_CH_USER:-default}}"
CH_PASSWORD="${HFT_CLICKHOUSE_PASSWORD:-${MONITOR_CH_PASSWORD:-changeme}}"
REPLAY_TICKS="${HFT_MONITOR_REPLAY_TICKS:-${MONITOR_REPLAY_TICKS:-24}}"
BATCH_LIMIT_PER_SYMBOL="${HFT_MONITOR_BATCH_LIMIT_PER_SYMBOL:-${MONITOR_BATCH_LIMIT_PER_SYMBOL:-96}}"
REDIS_HOST="${HFT_MONITOR_REDIS_HOST:-${MONITOR_REDIS_HOST:-127.0.0.1}}"
REDIS_PORT="${HFT_MONITOR_REDIS_PORT:-${MONITOR_REDIS_PORT:-6379}}"
REDIS_PASSWORD="${HFT_MONITOR_REDIS_PASSWORD:-${MONITOR_REDIS_PASSWORD:-}}"
UV_SYNC="${MONITOR_UV_SYNC:-1}"

echo "Signal Monitor TUI"
echo "Remote: ${REMOTE_USER}@${REMOTE_HOST}"
echo "Source: ${SOURCE}"

if [ "$UV_SYNC" = "1" ]; then
    echo "Syncing monitor dependencies..."
    uv sync --extra monitor >/dev/null
fi

export HFT_MONITOR_SOURCE="${SOURCE}"
export HFT_MONITOR_REPLAY_TICKS="${REPLAY_TICKS}"
export HFT_MONITOR_BATCH_LIMIT_PER_SYMBOL="${BATCH_LIMIT_PER_SYMBOL}"

if [ "$SOURCE" = "clickhouse" ]; then
    echo "Tunnel: 127.0.0.1:${TUNNEL_PORT} -> localhost:${REMOTE_CH_PORT}"
    if curl -fsS "http://127.0.0.1:${TUNNEL_PORT}/ping" >/dev/null 2>&1; then
        echo "Using existing ClickHouse tunnel on ${TUNNEL_PORT}."
    else
        echo "Opening SSH tunnel..."
        ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -f -N \
            -L "${TUNNEL_PORT}:localhost:${REMOTE_CH_PORT}" \
            "${REMOTE_USER}@${REMOTE_HOST}"
    fi

    if ! curl -fsS "http://127.0.0.1:${TUNNEL_PORT}/ping" >/dev/null 2>&1; then
        echo "Failed to reach ClickHouse through tunnel on ${TUNNEL_PORT}." >&2
        echo "Check SSH reachability or free the local port and retry." >&2
        exit 1
    fi

    export HFT_CLICKHOUSE_HOST="127.0.0.1"
    export HFT_CLICKHOUSE_PORT="${TUNNEL_PORT}"
    export HFT_CLICKHOUSE_USER="${CH_USER}"
    export HFT_CLICKHOUSE_PASSWORD="${CH_PASSWORD}"
else
    export HFT_MONITOR_REDIS_HOST="${REDIS_HOST}"
    export HFT_MONITOR_REDIS_PORT="${REDIS_PORT}"
    export HFT_MONITOR_REDIS_PASSWORD="${REDIS_PASSWORD}"
fi

echo "Starting monitor..."
echo "Replay ticks: ${HFT_MONITOR_REPLAY_TICKS} | Batch per symbol: ${HFT_MONITOR_BATCH_LIMIT_PER_SYMBOL}"
exec uv run hft monitor --watchlist "${WATCHLIST_PATH}" --symbols "${SYMBOLS_PATH}"
