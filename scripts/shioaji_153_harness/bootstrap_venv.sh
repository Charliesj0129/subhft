#!/usr/bin/env bash
# bootstrap_venv.sh — Phase 0 of the Shioaji 1.5.3 validation plan.
#
# Builds an ISOLATED throwaway venv under /tmp containing the repo's locked
# dependency set with shioaji bumped 1.3.3 -> 1.5.3[speed], so the held PR #367
# can be exercised at runtime WITHOUT touching the project .venv / pyproject.toml
# pin / uv.lock. The in-tree editable `hft_platform` + in-tree compiled
# `rust_core.so` are reused via PYTHONPATH (no maturin rebuild).
#
# Invariants enforced (fail-closed):
#   * pyproject.toml / uv.lock / .venv/pyvenv.cfg sha256 unchanged across the run.
#   * `git status --porcelain` unchanged across the run (run mutates nothing tracked).
#   * The venv lives ONLY under /tmp, never inside the repo.
#   * Reads uv.lock (via `uv export`); never writes it.
#
# Usage:  bash scripts/shioaji_153_harness/bootstrap_venv.sh
#         SHIOAJI_HARNESS_VERSION=1.5.5 bash scripts/shioaji_153_harness/bootstrap_venv.sh
#
# Re-runnable: pass FORCE=1 to wipe an existing harness venv first.
set -euo pipefail
set -o noclobber

# --------------------------------------------------------------------------- #
# Paths — repo root resolved from this script; all outputs ABSOLUTE under /tmp.
# Target SDK version is parameterized (2026-07-08 retarget to 1.5.5); each
# version gets its own /tmp harness dir so runs never cross-contaminate.
# --------------------------------------------------------------------------- #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
HARNESS_VER="${SHIOAJI_HARNESS_VERSION:-1.5.3}"
HARNESS_DIR="/tmp/shioaji_$(printf '%s' "${HARNESS_VER}" | tr '.' '_')_harness"
VENV="${HARNESS_DIR}/venv"
REQ_LOCKED="${HARNESS_DIR}/requirements.locked.txt"
REQ_NOSHIOAJI="${HARNESS_DIR}/requirements.no-shioaji.txt"
FREEZE_OUT="${HARNESS_DIR}/pip-freeze.${HARNESS_VER}.txt"
DELTA_OUT="${HARNESS_DIR}/freeze-delta.txt"

SHIOAJI_TARGET="shioaji[speed]==${HARNESS_VER}"
PY_VERSION="3.12"

log() { printf '[bootstrap] %s\n' "$*" >&2; }
die() { printf '[bootstrap][FATAL] %s\n' "$*" >&2; exit 1; }

case "${VENV}" in
  "${REPO_ROOT}"/*) die "refusing to create a venv inside the repo: ${VENV}" ;;
  /tmp/*) : ;;
  *) die "venv must live under /tmp, got: ${VENV}" ;;
esac

# --------------------------------------------------------------------------- #
# Invariant snapshot — BEFORE.
# --------------------------------------------------------------------------- #
sha_of() { [ -f "$1" ] && sha256sum "$1" | awk '{print $1}' || echo "MISSING"; }

PYPROJECT="${REPO_ROOT}/pyproject.toml"
UVLOCK="${REPO_ROOT}/uv.lock"
PYVENV_CFG="${REPO_ROOT}/.venv/pyvenv.cfg"

BEFORE_PYPROJECT="$(sha_of "${PYPROJECT}")"
BEFORE_UVLOCK="$(sha_of "${UVLOCK}")"
BEFORE_PYVENV="$(sha_of "${PYVENV_CFG}")"
BEFORE_GITSTATUS="$(cd "${REPO_ROOT}" && git status --porcelain)"

log "invariant baseline captured (pyproject/uv.lock/.venv/pyvenv.cfg + git status)"

# --------------------------------------------------------------------------- #
# Step 1 — export the frozen lockset (reads uv.lock, writes only to /tmp).
# --------------------------------------------------------------------------- #
mkdir -p "${HARNESS_DIR}"
if [ -d "${VENV}" ]; then
  if [ "${FORCE:-0}" = "1" ]; then
    log "FORCE=1 — removing existing venv ${VENV}"
    rm -rf "${VENV}"
  else
    die "venv already exists at ${VENV} (re-run with FORCE=1 to rebuild)"
  fi
fi

log "exporting frozen lockset via uv export (no project, no dev)…"
# noclobber blocks '>' onto an existing file; remove stale artifacts first.
rm -f "${REQ_LOCKED}" "${REQ_NOSHIOAJI}" "${FREEZE_OUT}" "${DELTA_OUT}"
( cd "${REPO_ROOT}" && uv export --frozen --no-emit-project --no-dev ) > "${REQ_LOCKED}"

# --------------------------------------------------------------------------- #
# Step 2 — strip the shioaji requirement block (name line + its indented hash /
# marker continuation lines) so 1.5.3 can be installed cleanly on top.
# --------------------------------------------------------------------------- #
log "stripping shioaji block from the locked requirements…"
python3 - "${REQ_LOCKED}" "${REQ_NOSHIOAJI}" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
out, skip = [], False
for line in open(src, encoding="utf-8"):
    stripped = line.rstrip("\n")
    # A new requirement block starts at a non-indented, non-comment line.
    is_block_start = bool(stripped) and not stripped[0].isspace() and not stripped.startswith("#")
    if is_block_start:
        name = stripped.split("==", 1)[0].split(" ", 1)[0].split("[", 1)[0].strip().lower()
        skip = (name == "shioaji")
    if skip:
        continue
    out.append(line)
open(dst, "w", encoding="utf-8").write("".join(out))
PY

if grep -qiE '^shioaji([[:space:]]|==|\[)' "${REQ_NOSHIOAJI}"; then
  die "shioaji block survived the strip — aborting"
fi
log "stripped lockset written: ${REQ_NOSHIOAJI}"

# --------------------------------------------------------------------------- #
# Step 3 — create the isolated venv and install (locked deps, then shioaji 1.5.3).
# --------------------------------------------------------------------------- #
log "creating isolated venv at ${VENV} (python ${PY_VERSION})…"
uv venv "${VENV}" --python "${PY_VERSION}"

VENV_PY="${VENV}/bin/python"
log "installing locked deps (hash-checked) minus shioaji…"
uv pip install --python "${VENV_PY}" -r "${REQ_NOSHIOAJI}"

log "installing ${SHIOAJI_TARGET} on top…"
uv pip install --python "${VENV_PY}" "${SHIOAJI_TARGET}"

# --------------------------------------------------------------------------- #
# Step 4 — smoke test: in-tree hft_platform + rust_core import, shioaji matches
# the target version. PYTHONPATH points at the in-tree src; never prepend the
# project .venv.
# --------------------------------------------------------------------------- #
log "smoke test (hft_platform.rust_core + normalizer + shioaji ${HARNESS_VER})…"
PYTHONPATH="${REPO_ROOT}/src" SHIOAJI_EXPECTED_VER="${HARNESS_VER}" "${VENV_PY}" - <<'PY'
import importlib
import os

import hft_platform.rust_core  # in-tree compiled .so
import hft_platform.feed_adapter.normalizer  # must NOT need the SDK
import shioaji

expected = os.environ["SHIOAJI_EXPECTED_VER"]
assert shioaji.__version__ == expected, f"expected shioaji {expected}, got {shioaji.__version__}"
# Confirm the Rust-extension rewrite is actually loaded (1.5.x ships _core.abi3.so).
core = importlib.import_module("shioaji._core")
print(f"OK shioaji={shioaji.__version__} _core={getattr(core, '__file__', '<builtin>')}")
print(f"OK rust_core={hft_platform.rust_core.__file__}")
PY

# --------------------------------------------------------------------------- #
# Step 5 — freeze-delta report vs the locked set (flag confounders).
# --------------------------------------------------------------------------- #
log "computing freeze delta vs locked set…"
uv pip freeze --python "${VENV_PY}" > "${FREEZE_OUT}"
python3 - "${REQ_LOCKED}" "${FREEZE_OUT}" "${DELTA_OUT}" "${HARNESS_VER}" <<'PY'
import sys

locked_path, freeze_path, delta_path, harness_ver = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]


def parse_locked(path):
    pins = {}
    for line in open(path, encoding="utf-8"):
        s = line.rstrip("\n")
        if not s or s[0].isspace() or s.startswith("#"):
            continue
        if "==" not in s:
            continue
        name = s.split("==", 1)[0].split(" ", 1)[0].split("[", 1)[0].strip().lower()
        ver = s.split("==", 1)[1].split(" ", 1)[0].strip().rstrip(" \\")
        pins[name] = ver
    return pins


def parse_freeze(path):
    pins = {}
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-e"):
            continue
        if "==" not in s:
            continue
        name, ver = s.split("==", 1)
        pins[name.split("[", 1)[0].strip().lower()] = ver.strip()
    return pins


locked = parse_locked(locked_path)
installed = parse_freeze(freeze_path)
# shioaji intentionally differs (the whole point); list it but do not flag it.
CONFOUNDERS = {"numpy", "pydantic", "protobuf"}

added, removed, changed = [], [], []
for name, ver in sorted(installed.items()):
    if name not in locked:
        added.append((name, ver))
    elif locked[name] != ver:
        changed.append((name, locked[name], ver))
for name in sorted(locked):
    if name not in installed:
        removed.append((name, locked[name]))

lines = [f"# Freeze delta: harness venv (shioaji {harness_ver}) vs repo locked set", ""]
lines.append(f"locked packages: {len(locked)}  installed packages: {len(installed)}")
lines.append("")
lines.append("## Changed versions")
flagged = []
for name, old, new in changed:
    mark = "  <-- CONFOUNDER" if name in CONFOUNDERS else ""
    if name in CONFOUNDERS:
        flagged.append(name)
    lines.append(f"  {name}: {old} -> {new}{mark}")
if not changed:
    lines.append("  (none)")
lines.append("")
lines.append(f"## Added (transitively pulled by shioaji {harness_ver})")
for name, ver in added:
    mark = "  <-- CONFOUNDER" if name in CONFOUNDERS else ""
    if name in CONFOUNDERS:
        flagged.append(name)
    lines.append(f"  {name}=={ver}{mark}")
if not added:
    lines.append("  (none)")
lines.append("")
lines.append("## Removed (locked but absent after install)")
for name, ver in removed:
    mark = "  <-- CONFOUNDER" if name in CONFOUNDERS else ""
    if name in CONFOUNDERS:
        flagged.append(name)
    lines.append(f"  {name} (was {ver}){mark}")
if not removed:
    lines.append("  (none)")
lines.append("")
if flagged:
    lines.append(f"WARNING: perf-confounding packages diverged: {sorted(set(flagged))}")
else:
    lines.append("OK: no numpy/pydantic/protobuf divergence (perf comparison is clean).")

text = "\n".join(lines) + "\n"
open(delta_path, "w", encoding="utf-8").write(text)
print(text)
PY

# --------------------------------------------------------------------------- #
# Invariant snapshot — AFTER. Any drift is fail-closed.
# --------------------------------------------------------------------------- #
AFTER_PYPROJECT="$(sha_of "${PYPROJECT}")"
AFTER_UVLOCK="$(sha_of "${UVLOCK}")"
AFTER_PYVENV="$(sha_of "${PYVENV_CFG}")"
AFTER_GITSTATUS="$(cd "${REPO_ROOT}" && git status --porcelain)"

drift=0
[ "${BEFORE_PYPROJECT}" = "${AFTER_PYPROJECT}" ] || { log "DRIFT: pyproject.toml sha changed"; drift=1; }
[ "${BEFORE_UVLOCK}" = "${AFTER_UVLOCK}" ] || { log "DRIFT: uv.lock sha changed"; drift=1; }
[ "${BEFORE_PYVENV}" = "${AFTER_PYVENV}" ] || { log "DRIFT: .venv/pyvenv.cfg sha changed"; drift=1; }
if [ "${BEFORE_GITSTATUS}" != "${AFTER_GITSTATUS}" ]; then
  log "DRIFT: git status --porcelain changed during run"
  diff <(printf '%s\n' "${BEFORE_GITSTATUS}") <(printf '%s\n' "${AFTER_GITSTATUS}") >&2 || true
  drift=1
fi
[ "${drift}" -eq 0 ] || die "invariant drift detected — see above"

log "INVARIANTS HOLD: pyproject/uv.lock/.venv/pyvenv.cfg + git status unchanged"
log "DONE. venv=${VENV}"
log "  run-with:  PYTHONPATH=${REPO_ROOT}/src ${VENV_PY} <script>"
log "  freeze:    ${FREEZE_OUT}"
log "  delta:     ${DELTA_OUT}"
