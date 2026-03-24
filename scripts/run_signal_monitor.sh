#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REMOTE_USER="${MONITOR_REMOTE_USER:?Set MONITOR_REMOTE_USER}"
REMOTE_HOST="${MONITOR_REMOTE_HOST:?Set MONITOR_REMOTE_HOST}"
TUNNEL_PORT="${MONITOR_TUNNEL_PORT:-18123}"
REMOTE_CH_PORT="${MONITOR_REMOTE_CH_PORT:-8123}"
WATCHLIST_PATH="${MONITOR_WATCHLIST:-config/watchlist.yaml}"
SYMBOLS_PATH="${MONITOR_SYMBOLS_PATH:-config/symbols.yaml}"
SOURCE="${HFT_MONITOR_SOURCE:-${MONITOR_SOURCE:-redis}}"
DATA_SOURCE="${HFT_MONITOR_DATA_SOURCE:-${MONITOR_DATA_SOURCE:-}}"
CH_USER="${HFT_CLICKHOUSE_USER:-${MONITOR_CH_USER:-default}}"
CH_PASSWORD="${HFT_CLICKHOUSE_PASSWORD:-${MONITOR_CH_PASSWORD:-${CLICKHOUSE_PASSWORD:-}}}"
REPLAY_TICKS="${HFT_MONITOR_REPLAY_TICKS:-${MONITOR_REPLAY_TICKS:-24}}"
BATCH_LIMIT_PER_SYMBOL="${HFT_MONITOR_BATCH_LIMIT_PER_SYMBOL:-${MONITOR_BATCH_LIMIT_PER_SYMBOL:-96}}"
REDIS_TUNNEL_PORT="${MONITOR_REDIS_TUNNEL_PORT:-16379}"
REMOTE_REDIS_PORT="${MONITOR_REMOTE_REDIS_PORT:-6379}"
REDIS_HOST="${HFT_MONITOR_REDIS_HOST:-${MONITOR_REDIS_HOST:-127.0.0.1}}"
REDIS_PORT="${HFT_MONITOR_REDIS_PORT:-${MONITOR_REDIS_PORT:-${REDIS_TUNNEL_PORT}}}"
REDIS_PASSWORD="${HFT_MONITOR_REDIS_PASSWORD:-${MONITOR_REDIS_PASSWORD:-${REDIS_PASSWORD:-}}}"
UV_SYNC="${MONITOR_UV_SYNC:-1}"

echo "Signal Monitor TUI"
echo "Remote: ${REMOTE_USER}@${REMOTE_HOST}"
echo "Source: ${SOURCE}"

if [ -z "${DATA_SOURCE}" ]; then
    if [ "$SOURCE" = "redis" ]; then
        DATA_SOURCE="ch"
    else
        DATA_SOURCE="auto"
    fi
fi

if [ "$UV_SYNC" = "1" ]; then
    echo "Syncing monitor dependencies..."
    uv sync --extra monitor >/dev/null
fi

export HFT_MONITOR_SOURCE="${SOURCE}"
export HFT_MONITOR_DATA_SOURCE="${DATA_SOURCE}"
export HFT_MONITOR_REPLAY_TICKS="${REPLAY_TICKS}"
export HFT_MONITOR_BATCH_LIMIT_PER_SYMBOL="${BATCH_LIMIT_PER_SYMBOL}"

# --- SSH Tunnels ---
# Helper: ensure an SSH tunnel is open
open_tunnel() {
    local local_port=$1 remote_port=$2 label=$3
    if ss -tlnp 2>/dev/null | grep -q ":${local_port} " || \
       lsof -iTCP:${local_port} -sTCP:LISTEN >/dev/null 2>&1; then
        echo "Using existing ${label} tunnel on ${local_port}."
    else
        echo "Opening ${label} SSH tunnel: 127.0.0.1:${local_port} -> localhost:${remote_port}"
        ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -f -N \
            -L "${local_port}:localhost:${remote_port}" \
            "${REMOTE_USER}@${REMOTE_HOST}"
        # Wait for tunnel to become ready (up to 5s)
        local retries=0
        while [ $retries -lt 10 ]; do
            if ss -tlnp 2>/dev/null | grep -q ":${local_port} " || \
               lsof -iTCP:${local_port} -sTCP:LISTEN >/dev/null 2>&1; then
                echo "${label} tunnel ready on ${local_port}."
                break
            fi
            retries=$((retries + 1))
            sleep 0.5
        done
    fi
}

# Open tunnels based on source mode
if [ "$SOURCE" = "clickhouse" ] || [ "$SOURCE" = "hybrid" ]; then
    open_tunnel "${TUNNEL_PORT}" "${REMOTE_CH_PORT}" "ClickHouse"
    export HFT_CLICKHOUSE_HOST="127.0.0.1"
    export HFT_CLICKHOUSE_PORT="${TUNNEL_PORT}"
    export HFT_CLICKHOUSE_USER="${CH_USER}"
    export HFT_CLICKHOUSE_PASSWORD="${CH_PASSWORD}"
fi

if [ "$SOURCE" = "redis" ] || [ "$SOURCE" = "hybrid" ]; then
    open_tunnel "${REDIS_TUNNEL_PORT}" "${REMOTE_REDIS_PORT}" "Redis"
    export HFT_MONITOR_REDIS_HOST="127.0.0.1"
    export HFT_MONITOR_REDIS_PORT="${REDIS_TUNNEL_PORT}"
    export HFT_MONITOR_REDIS_PASSWORD="${REDIS_PASSWORD}"
fi

echo "Starting monitor..."
echo "Replay ticks: ${HFT_MONITOR_REPLAY_TICKS} | Batch per symbol: ${HFT_MONITOR_BATCH_LIMIT_PER_SYMBOL}"
exec uv run hft monitor --watchlist "${WATCHLIST_PATH}" --symbols "${SYMBOLS_PATH}"
