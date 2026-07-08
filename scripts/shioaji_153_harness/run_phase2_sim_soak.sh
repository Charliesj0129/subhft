#!/usr/bin/env bash
# run_phase2_sim_soak.sh — Phase 2 of the Shioaji 1.5.3 validation plan.
#
# Drives the CREDENTIALED SIM soak (scripts/latency/shioaji_sim_soak.py) inside
# the isolated 1.5.3 harness venv: measured place/update/cancel latency P50/P95/
# P99, real-payload Decimal->scaled-int parity, reconnect-event timing, and
# RSS/thread/fd growth. Produces the runtime evidence that clears the last
# residuals on the held PR #367.
#
# SAFETY (the driver also enforces these; the shell adds defense-in-depth):
#   * `set +x` — never trace commands (would leak env). Creds come from
#     SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY in the environment ONLY, never args.
#   * Refuses if any SHIOAJI_CA* (live-cert) var is present — simulation only.
#   * Output JSON lands under outputs/ (gitignored); contains no credentials.
#
# Usage:
#   export SHIOAJI_API_KEY=...  SHIOAJI_SECRET_KEY=...   # simulation creds
#   bash scripts/shioaji_153_harness/run_phase2_sim_soak.sh --minutes 30
set -euo pipefail
set +x  # never echo commands — env may hold the sim secret.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
HARNESS_VER="${SHIOAJI_HARNESS_VERSION:-1.5.3}"
HARNESS_DIR="/tmp/shioaji_$(printf '%s' "${HARNESS_VER}" | tr '.' '_')_harness"
VENV_PY="${HARNESS_DIR}/venv/bin/python"

MINUTES="30"
EXTRA_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --minutes) MINUTES="$2"; shift 2 ;;
    --minutes=*) MINUTES="${1#*=}"; shift ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done

log() { printf '[phase2] %s\n' "$*" >&2; }
die() { printf '[phase2][FATAL] %s\n' "$*" >&2; exit 1; }

[ -x "${VENV_PY}" ] || die "harness venv missing — run bootstrap_venv.sh first"

# Fail-closed safety checks (mirrors the driver; fails before any login).
if compgen -A variable | grep -q '^SHIOAJI_CA'; then
  die "live CA env vars present — refusing (simulation only). Unset SHIOAJI_CA*."
fi
[ -n "${SHIOAJI_API_KEY:-}" ] || die "SHIOAJI_API_KEY not set (simulation creds, env only)"
[ -n "${SHIOAJI_SECRET_KEY:-}" ] || die "SHIOAJI_SECRET_KEY not set (simulation creds, env only)"

INSTALLED_VER="$(PYTHONPATH="${REPO_ROOT}/src" "${VENV_PY}" -c 'import shioaji; print(shioaji.__version__)')"
[ "${INSTALLED_VER}" = "${HARNESS_VER}" ] || die "harness venv has shioaji ${INSTALLED_VER}, expected ${HARNESS_VER}"
log "harness venv confirmed: shioaji ${INSTALLED_VER}; sim creds present (not echoed)"

# Timestamped output path (gitignored under outputs/).
TS="$(date +%Y%m%d_%H%M%S)"
OUT="${REPO_ROOT}/outputs/shioaji_153_sim_soak_${TS}.json"
mkdir -p "${REPO_ROOT}/outputs"

# Invariant baseline (repo tree must not change).
sha_of() { [ -f "$1" ] && sha256sum "$1" | awk '{print $1}' || echo "MISSING"; }
B_PYPROJECT="$(sha_of "${REPO_ROOT}/pyproject.toml")"
B_UVLOCK="$(sha_of "${REPO_ROOT}/uv.lock")"

log "starting ${MINUTES}-minute SIM soak → ${OUT}"
PYTHONPATH="${REPO_ROOT}/src" "${VENV_PY}" "${REPO_ROOT}/scripts/latency/shioaji_sim_soak.py" \
  --minutes "${MINUTES}" --out "${OUT}" "${EXTRA_ARGS[@]}"

# Invariant guard.
[ "${B_PYPROJECT}" = "$(sha_of "${REPO_ROOT}/pyproject.toml")" ] || die "DRIFT: pyproject.toml changed"
[ "${B_UVLOCK}" = "$(sha_of "${REPO_ROOT}/uv.lock")" ] || die "DRIFT: uv.lock changed"

log "DONE. Evidence: ${OUT}"
log "Draft profile: ${OUT%.json}.profile.yaml"
log "GO/No-Go: compare P95 place/update/cancel vs baseline shioaji_sim_p95_v2026-03-04"
log "          (36/43/47 ms); GO iff ${HARNESS_VER} P95 <= baseline x1.2 AND decimal_parity.parity_holds"
log "          AND resources slopes ~0 AND fills_aborted==0."
