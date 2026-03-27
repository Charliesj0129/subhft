#!/usr/bin/env bash
# scripts/research_data_rotate.sh — 4-tier research data rotation per data-retention-policy.md
# Cron: 0 4 * * 0 (weekly, Sunday 04:00)
set -euo pipefail

RAW_RETAIN_DAYS="${RESEARCH_RAW_RETAIN_DAYS:-90}"
ARCHIVE_RETAIN_DAYS="${RESEARCH_ARCHIVE_RETAIN_DAYS:-180}"
RUNS_RETAIN_DAYS="${RESEARCH_RUNS_RETAIN_DAYS:-90}"
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node-exporter/textfile}"
DRY_RUN="${1:-}"

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${BASE_DIR}/research/data"
RUNS_DIR="${BASE_DIR}/research/experiments/runs"
ARCHIVE_DIR="${DATA_DIR}/archive"

log() { echo "[$(date -Iseconds)] $*"; }

run_or_print() {
    if [ "$DRY_RUN" = "--dry-run" ]; then
        log "[DRY-RUN] $*"
    else
        "$@"
    fi
}

# --- Tier 1: raw/ — archive after RAW_RETAIN_DAYS, delete archive after ARCHIVE_RETAIN_DAYS ---
log "=== Tier 1: research/data/raw/ ==="
mkdir -p "$ARCHIVE_DIR"
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

# --- Tier 2: processed/ — delete inactive alpha dirs older than 90 days ---
# Protected: research/data/processed/smoke/smoke_v1.npy (must-keep per data-retention-policy)
log "=== Tier 2: research/data/processed/ ==="
if [ -d "${DATA_DIR}/processed" ]; then
    find "${DATA_DIR}/processed" -mindepth 1 -maxdepth 1 -type d -mtime "+${RAW_RETAIN_DAYS}" | while read -r dir; do
        dirname="$(basename "$dir")"
        if [ "$dirname" = "smoke" ]; then
            log "PROTECTED: $dir (must-keep)"
            continue
        fi
        log "Removing stale processed dir: $dir"
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

# --- Tier 4: experiments/runs/ — delete after RUNS_RETAIN_DAYS ---
log "=== Tier 4: research/experiments/runs/ ==="
if [ -d "$RUNS_DIR" ]; then
    find "$RUNS_DIR" -mindepth 1 -maxdepth 1 -type d -mtime "+${RUNS_RETAIN_DAYS}" | while read -r dir; do
        log "Removing old experiment run: $dir"
        run_or_print rm -rf "$dir"
    done
fi

# --- Emit Prometheus textfile metric ---
if [ "$DRY_RUN" != "--dry-run" ] && [ -d "$TEXTFILE_DIR" ]; then
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
