#!/usr/bin/env bash
# run_phase1.sh — Phase 1 of the Shioaji 1.5.3 validation plan: OFFLINE,
# credential-free evidence in the isolated 1.5.3 harness venv.
#
#   1A  Existing adapter unit suite + the SDK surface-golden guard, run against
#       the REAL installed shioaji 1.5.3 (proves adapter logic + that the
#       installed surface matches the committed surface_1.5.3.json snapshot).
#   1B  The Decimal->scaled-int boundary guard (the #367-critical invariant;
#       SDK-free, runs identically in both venvs).
#   1C  Offline perf: the established perf-regression gate + the new Decimal
#       normalize bench (relative gates), plus informational micro-benches.
#
# Requires Phase 0 (bootstrap_venv.sh) to have built /tmp/shioaji_1_5_3_harness/venv.
# Touches ONLY the /tmp harness venv; the repo working tree, project .venv,
# pyproject.toml and uv.lock are invariant across the run (fail-closed guard).
#
# Usage:  bash scripts/shioaji_153_harness/run_phase1.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
HARNESS_DIR="/tmp/shioaji_1_5_3_harness"
VENV="${HARNESS_DIR}/venv"
VENV_PY="${VENV}/bin/python"
OUT="${HARNESS_DIR}/out/phase1"

log() { printf '[phase1] %s\n' "$*" >&2; }
die() { printf '[phase1][FATAL] %s\n' "$*" >&2; exit 1; }

[ -x "${VENV_PY}" ] || die "harness venv missing — run bootstrap_venv.sh first (${VENV_PY})"
mkdir -p "${OUT}"

# Confirm we are really pointed at 1.5.3 before spending time.
INSTALLED_VER="$(PYTHONPATH="${REPO_ROOT}/src" "${VENV_PY}" -c 'import shioaji; print(shioaji.__version__)')"
[ "${INSTALLED_VER}" = "1.5.3" ] || die "harness venv has shioaji ${INSTALLED_VER}, expected 1.5.3"
log "harness venv confirmed: shioaji ${INSTALLED_VER}"

# --------------------------------------------------------------------------- #
# Invariant baseline (repo tree must not change during the run).
# --------------------------------------------------------------------------- #
sha_of() { [ -f "$1" ] && sha256sum "$1" | awk '{print $1}' || echo "MISSING"; }
B_PYPROJECT="$(sha_of "${REPO_ROOT}/pyproject.toml")"
B_UVLOCK="$(sha_of "${REPO_ROOT}/uv.lock")"
B_PYVENV="$(sha_of "${REPO_ROOT}/.venv/pyvenv.cfg")"
B_GIT="$(cd "${REPO_ROOT}" && git status --porcelain)"

# --------------------------------------------------------------------------- #
# Test toolchain — pinned to uv.lock versions, installed ONLY in the /tmp venv.
# These are test-only and do not change shioaji's runtime object shape.
# --------------------------------------------------------------------------- #
if ! "${VENV_PY}" -c 'import pytest' >/dev/null 2>&1; then
  log "installing pinned test toolchain into the harness venv…"
  uv pip install --python "${VENV_PY}" \
    'pytest==9.0.1' 'pytest-asyncio==1.3.0' 'pytest-timeout==2.4.0' \
    'pytest-cov==7.0.0' 'hypothesis==6.152.4' 'pytest-benchmark>=4.0.0' >&2
else
  log "test toolchain already present"
fi

# Common pytest flags: clear repo addopts (drops --cov gate / --timeout / ignore)
# and disable the cache writer so nothing lands in the repo tree.
PYTEST_BASE=(-o addopts= -p no:cacheprovider --timeout=120 -q)
run_py() { PYTHONPATH="${REPO_ROOT}/src" "${VENV_PY}" "$@"; }

overall=0
fail() { overall=1; log "STEP FAILED: $*"; }

# --------------------------------------------------------------------------- #
# 1A + 1B — adapter unit suite + surface golden + Decimal boundary.
# --------------------------------------------------------------------------- #
log "1A/1B: unit suite + surface golden + Decimal boundary vs real 1.5.3…"
set +e
run_py -m pytest \
  tests/unit/feed_adapter/shioaji/ \
  tests/unit/test_shioaji_account_gateway.py \
  tests/unit/test_shioaji_callback_routing.py \
  tests/unit/test_shioaji_config.py \
  tests/unit/test_shioaji_contract_refresh.py \
  tests/unit/test_shioaji_facade.py \
  tests/unit/test_shioaji_family_populator.py \
  tests/unit/test_shioaji_full_mock.py \
  tests/unit/test_shioaji_historical_gateway.py \
  tests/unit/test_shioaji_infra.py \
  tests/unit/test_shioaji_market_info_gateway.py \
  tests/unit/test_shioaji_metrics_bridge.py \
  tests/unit/test_shioaji_order_codec.py \
  tests/unit/test_shioaji_order_gateway.py \
  tests/unit/test_shioaji_reconnect_orchestrator.py \
  tests/unit/test_shioaji_scanner_gateway.py \
  tests/unit/test_shioaji_session_runtime_extended.py \
  tests/unit/test_shioaji_solace_arity_shim.py \
  tests/unit/test_shioaji_subscription_manager.py \
  tests/unit/test_shioaji_thread_lifecycle.py \
  tests/unit/test_shioaji_tick_dispatcher.py \
  tests/unit/test_shioaji_tiered_rate_limiter.py \
  "${PYTEST_BASE[@]}" 2>&1 | tee "${OUT}/pytest_unit.log"
rc=${PIPESTATUS[0]}
set -e
[ "${rc}" -eq 0 ] || fail "unit suite / surface golden (rc=${rc})"

# --------------------------------------------------------------------------- #
# 1C — offline perf.
# --------------------------------------------------------------------------- #
log "1C: Decimal normalize bench (relative gate)…"
set +e
run_py tests/benchmark/bench_shioaji_normalize_decimal.py --check --iters 50000 \
  2>&1 | tee "${OUT}/bench_normalize_decimal.log"
rc=${PIPESTATUS[0]}; set -e
[ "${rc}" -eq 0 ] || fail "Decimal normalize bench (rc=${rc})"
run_py tests/benchmark/bench_shioaji_normalize_decimal.py --json --iters 50000 \
  > "${OUT}/bench_normalize_decimal.json" 2>/dev/null || true

log "1C: perf-regression gate vs perf_baselines.json…"
set +e
run_py tests/benchmark/perf_regression_gate.py \
  --baseline tests/benchmark/perf_baselines.json \
  --json "${OUT}/perf_gate.json" --runs 3 2>&1 | tee "${OUT}/perf_gate.log"
rc=${PIPESTATUS[0]}; set -e
[ "${rc}" -eq 0 ] || fail "perf-regression gate (rc=${rc})"

log "1C: informational micro-benches (not gated)…"
run_py tests/benchmark/micro_bench_shioaji_callback_dispatch.py > "${OUT}/micro_callback.log" 2>&1 || true
run_py tests/benchmark/micro_bench_normalizer.py > "${OUT}/micro_normalizer.log" 2>&1 || true
run_py tests/benchmark/soak_shioaji_callback_dispatch.py --seconds 3 > "${OUT}/soak_callback.log" 2>&1 || true

# --------------------------------------------------------------------------- #
# Invariant guard.
# --------------------------------------------------------------------------- #
A_GIT="$(cd "${REPO_ROOT}" && git status --porcelain)"
drift=0
[ "${B_PYPROJECT}" = "$(sha_of "${REPO_ROOT}/pyproject.toml")" ] || { log "DRIFT: pyproject.toml"; drift=1; }
[ "${B_UVLOCK}" = "$(sha_of "${REPO_ROOT}/uv.lock")" ] || { log "DRIFT: uv.lock"; drift=1; }
[ "${B_PYVENV}" = "$(sha_of "${REPO_ROOT}/.venv/pyvenv.cfg")" ] || { log "DRIFT: .venv/pyvenv.cfg"; drift=1; }
if [ "${B_GIT}" != "${A_GIT}" ]; then
  log "DRIFT: git status changed during run:"
  diff <(printf '%s\n' "${B_GIT}") <(printf '%s\n' "${A_GIT}") >&2 || true
  drift=1
fi
[ "${drift}" -eq 0 ] || die "invariant drift detected"
log "INVARIANTS HOLD"

echo "----------------------------------------------------------------" >&2
if [ "${overall}" -eq 0 ]; then
  log "PHASE 1 PASS — artifacts in ${OUT}"
else
  log "PHASE 1 HAS FAILURES — inspect logs in ${OUT}"
fi
exit "${overall}"
