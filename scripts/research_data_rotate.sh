#!/usr/bin/env bash
# scripts/research_data_rotate.sh — research data rotation per data-retention-policy.md
#
# Usage: research_data_rotate.sh [--dry-run | --apply]
#   no argument / --dry-run   print what would happen; nothing is archived, deleted or created
#   --apply                   really archive and delete (a typo such as --dryrun is refused, not treated as --apply)
#
# Tier 1 raw/        archive after RESEARCH_RAW_RETAIN_DAYS, delete archives after RESEARCH_ARCHIVE_RETAIN_DAYS
# Tier 2 processed/  remove a directory only when it is stale by FILE age (no file newer than the retention window)
#                    and its alpha is not GATE_B or above; an alpha status this script does not recognise protects it
# Tier 3 synthetic/  keep the newest file per sub-directory
# Tier 4 runs/       REPORT ONLY. research/experiments/** is immutable evidence (research/AGENTS.md); it is never
#                    deleted here. Use `make experiment-gc-dry-run` / gc_experiment_runs, which keep gate_c-passed
#                    and promoted runs.
#
# Cron (weekly, Sunday 04:00) must pass --apply to rotate: 0 4 * * 0 ... research_data_rotate.sh --apply
set -euo pipefail

RAW_RETAIN_DAYS="${RESEARCH_RAW_RETAIN_DAYS:-90}"
ARCHIVE_RETAIN_DAYS="${RESEARCH_ARCHIVE_RETAIN_DAYS:-180}"
RUNS_RETAIN_DAYS="${RESEARCH_RUNS_RETAIN_DAYS:-90}"
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node-exporter/textfile}"
# Reads alpha manifests; if it cannot import yaml, the status is "unknown" and the directory is protected.
ROTATE_PYTHON="${ROTATE_PYTHON:-python3}"

APPLY=0
usage() {
    echo "usage: $0 [--dry-run | --apply]" >&2
    exit 2
}
if [ "$#" -gt 1 ]; then
    usage
fi
case "${1:-}" in
    "" | --dry-run) ;;
    --apply) APPLY=1 ;;
    *) usage ;;
esac

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${BASE_DIR}/research/data"
RUNS_DIR="${BASE_DIR}/research/experiments/runs"
ARCHIVE_DIR="${DATA_DIR}/archive"

log() { echo "[$(date -Iseconds)] $*"; }

run_or_print() {
    if [ "$APPLY" = "1" ]; then
        "$@"
    else
        log "[DRY-RUN] $*"
    fi
}

# Prints the alpha's manifest status upper-cased, NONE when no manifest carries that id, UNKNOWN when it cannot be read.
alpha_status() {
    "$ROTATE_PYTHON" - "$BASE_DIR" "$1" 2>/dev/null <<'PY' || echo UNKNOWN
import sys
from pathlib import Path

base, alpha = Path(sys.argv[1]), sys.argv[2]
for root in ("research/alphas", "research/archive"):
    manifest = base / root / alpha / "manifest.yaml"
    if manifest.is_file():
        try:
            import yaml

            status = yaml.safe_load(manifest.read_text(encoding="utf-8")).get("status")
        except Exception:
            print("UNKNOWN")
            raise SystemExit(0)
        print(str(status).strip().upper() if status else "UNKNOWN")
        raise SystemExit(0)
print("NONE")
PY
}

if [ "$APPLY" = "1" ]; then
    log "mode: APPLY (archiving and deleting)"
else
    log "mode: DRY-RUN (nothing is archived, deleted or created; pass --apply to rotate)"
fi

# --- Tier 1: raw/ — archive after RAW_RETAIN_DAYS, delete archive after ARCHIVE_RETAIN_DAYS ---
log "=== Tier 1: research/data/raw/ ==="
run_or_print mkdir -p "$ARCHIVE_DIR"
if [ -d "${DATA_DIR}/raw" ]; then
    find "${DATA_DIR}/raw" -mindepth 1 -maxdepth 1 -type d -mtime "+${RAW_RETAIN_DAYS}" | while read -r dir; do
        archive_name="$(basename "$dir").tar.gz"
        log "Archiving: $dir → ${ARCHIVE_DIR}/${archive_name}"
        run_or_print tar czf "${ARCHIVE_DIR}/${archive_name}" -C "${DATA_DIR}/raw" "$(basename "$dir")"
        run_or_print rm -rf "$dir"
    done
fi
if [ -d "$ARCHIVE_DIR" ]; then
    find "$ARCHIVE_DIR" -name "*.tar.gz" -mtime "+${ARCHIVE_RETAIN_DAYS}" | while read -r f; do
        log "Deleting old archive: $f"
        run_or_print rm -f "$f"
    done
fi

# --- Tier 2: processed/ — remove a stale directory of an alpha that is not GATE_B or above ---
# A directory's own mtime only moves when an entry is added or removed, so a file rewritten in place does not refresh
# it. Staleness is therefore judged by the newest FILE inside, not by the directory.
# Protected: research/data/processed/smoke/smoke_v1.npy (must-keep per data-retention-policy)
log "=== Tier 2: research/data/processed/ ==="
if [ -d "${DATA_DIR}/processed" ]; then
    find "${DATA_DIR}/processed" -mindepth 1 -maxdepth 1 -type d -mtime "+${RAW_RETAIN_DAYS}" | while read -r dir; do
        dirname="$(basename "$dir")"
        if [ "$dirname" = "smoke" ]; then
            log "PROTECTED: $dir (must-keep)"
            continue
        fi
        if [ -n "$(find "$dir" -type f -mtime "-${RAW_RETAIN_DAYS}" -print -quit)" ]; then
            log "PROTECTED: $dir (a file inside was written within ${RAW_RETAIN_DAYS} days)"
            continue
        fi
        status="$(alpha_status "$dirname")"
        case "$status" in
            NONE | DRAFT | GATE_ZERO | PROTOTYPE | GATE_A | DEPRECATED | KILLED) ;;
            *)
                log "PROTECTED: $dir (alpha status: $status)"
                continue
                ;;
        esac
        log "Removing stale processed dir: $dir (alpha status: $status)"
        run_or_print rm -rf "$dir"
    done
fi

# --- Tier 3: synthetic/ — keep only latest version per sub-dir ---
log "=== Tier 3: research/data/synthetic/ ==="
if [ -d "${DATA_DIR}/synthetic" ]; then
    find "${DATA_DIR}/synthetic" -mindepth 1 -maxdepth 1 -type d | while read -r dir; do
        file_count=$(find "$dir" -maxdepth 1 -type f | wc -l)
        if [ "$file_count" -gt 1 ]; then
            log "Cleaning synthetic dir (keeping newest): $dir"
            ls -t "$dir" | tail -n +2 | while read -r old_file; do
                run_or_print rm -f "${dir}/${old_file}"
            done
        fi
    done
fi

# --- Tier 4: experiments/runs/ — REPORT ONLY (research/experiments/** is immutable evidence) ---
log "=== Tier 4: research/experiments/runs/ (report only, never deleted here) ==="
if [ -d "$RUNS_DIR" ]; then
    find "$RUNS_DIR" -mindepth 1 -maxdepth 1 -type d -mtime "+${RUNS_RETAIN_DAYS}" | while read -r dir; do
        log "[REPORT-ONLY] run older than ${RUNS_RETAIN_DAYS} days kept: $dir (make experiment-gc-dry-run lists what GC would remove)"
    done
fi

# --- Emit Prometheus textfile metric ---
if [ "$APPLY" = "1" ] && [ -d "$TEXTFILE_DIR" ]; then
    total_bytes=$(du -sb "${DATA_DIR}" 2>/dev/null | awk '{print $1}' || echo 0)
    runs_bytes=$(du -sb "${RUNS_DIR}" 2>/dev/null | awk '{print $1}' || echo 0)
    cat > "${TEXTFILE_DIR}/research_data.prom" <<METRICS
# HELP hft_research_data_bytes Total size of research/data/ in bytes
# TYPE hft_research_data_bytes gauge
hft_research_data_bytes ${total_bytes}
# HELP hft_research_runs_bytes Total size of research/experiments/runs/ in bytes
# TYPE hft_research_runs_bytes gauge
hft_research_runs_bytes ${runs_bytes}
METRICS
    log "Prometheus textfile written: ${total_bytes} bytes data, ${runs_bytes} bytes runs"
fi

log "=== Research data rotation complete ==="
