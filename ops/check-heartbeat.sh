#!/usr/bin/env bash
# Watchdog: restart the engine if the file heartbeat is stale.
#
# The engine writes ./.hft-runtime/heartbeat (bind-mounted to
# /var/run/hft/heartbeat in the container) once per ~30s from the supervisor
# loop. If that loop spins/starves, the file goes stale and this script
# restarts the engine.
#
# P3 fix (2026-06-15): the previous version restarted ONLY via `systemctl`,
# which silently no-ops on docker-compose hosts (e.g. THESHOW) — so a stale
# heartbeat there triggered nothing for ~18h. This version auto-detects the
# deploy mechanism: systemd unit -> docker compose -> plain docker.
set -euo pipefail

HEARTBEAT_FILE="${HFT_HEARTBEAT_FILE:-./.hft-runtime/heartbeat}"
MAX_AGE="${HFT_HEARTBEAT_MAX_AGE_S:-90}"
SERVICE="${HFT_ENGINE_SERVICE:-hft-engine}"          # systemd unit / compose service
CONTAINER="${HFT_ENGINE_CONTAINER:-hft-engine}"       # docker container name
COMPOSE_DIR="${HFT_COMPOSE_DIR:-.}"                    # dir holding the compose file

# Restart the engine using whichever mechanism this host actually runs.
# Returns 0 if a restart was dispatched, 1 if no mechanism was found.
restart_engine() {
    # 1) systemd unit (only if the unit is actually known to systemd).
    if command -v systemctl >/dev/null 2>&1 \
        && systemctl list-unit-files "${SERVICE}.service" 2>/dev/null | grep -q "${SERVICE}.service"; then
        echo "[watchdog] restarting via systemd: ${SERVICE}"
        systemctl restart "${SERVICE}" || echo "[watchdog] systemd restart returned non-zero"
        return 0
    fi
    # 2) docker compose (v2 plugin or legacy binary), run from the compose dir.
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        echo "[watchdog] restarting via docker compose: ${SERVICE}"
        ( cd "${COMPOSE_DIR}" && docker compose restart "${SERVICE}" ) \
            || echo "[watchdog] docker compose restart returned non-zero"
        return 0
    fi
    if command -v docker-compose >/dev/null 2>&1; then
        echo "[watchdog] restarting via docker-compose: ${SERVICE}"
        ( cd "${COMPOSE_DIR}" && docker-compose restart "${SERVICE}" ) \
            || echo "[watchdog] docker-compose restart returned non-zero"
        return 0
    fi
    # 3) plain docker by container name.
    if command -v docker >/dev/null 2>&1; then
        echo "[watchdog] restarting via docker: ${CONTAINER}"
        docker restart "${CONTAINER}" || echo "[watchdog] docker restart returned non-zero"
        return 0
    fi
    echo "[watchdog] ERROR: no restart mechanism found (systemctl/docker compose/docker)"
    return 1
}

if [ ! -f "$HEARTBEAT_FILE" ]; then
    echo "[watchdog] Heartbeat file missing ($HEARTBEAT_FILE) — restarting"
    restart_engine || true
    exit 1
fi

AGE=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT_FILE") ))

if [ "$AGE" -gt "$MAX_AGE" ]; then
    echo "[watchdog] Heartbeat stale (${AGE}s > ${MAX_AGE}s) — restarting"
    restart_engine || true
    exit 1
fi

echo "[watchdog] Heartbeat OK (age: ${AGE}s)"
exit 0
