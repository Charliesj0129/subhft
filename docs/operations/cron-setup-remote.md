# Remote Machine Cron Setup

## Context

The remote receiver machine (`charl@subhft`, 100.91.176.126) runs 24/7 without automated disk
maintenance. The 2026-03-02 disk crisis demonstrated that without periodic cleanup, a single
runaway log table can fill a 216 GB drive in days.

This document provides the crontab templates to establish automated housekeeping.

## Installation

On the remote machine, run:

```bash
crontab -e
```

Then paste the entries below (adjust paths as needed for your deployment root).

## Crontab Entries

```cron
# =============================================================================
# HFT Platform — Remote Machine Maintenance Crontab
# Deployment root: /home/charl/subhft
# Last updated: 2026-03-02
# =============================================================================

# --- WAL Archive Cleanup ---
# Delete WAL archive files older than 7 days (310 GB/year risk if unchecked).
# Conservative: change to 14 if long replay windows are needed for debugging.
0 2 * * 0 cd /home/charl/subhft && find .wal/archive -name "*.wal" -mtime +7 -delete >> /tmp/wal_cleanup.log 2>&1

# --- Docker Build Cache ---
# Docker layer cache can accumulate >10 GB/month during active development.
0 3 1 * * docker builder prune -f >> /tmp/docker_cleanup.log 2>&1

# --- Docker Unused Images ---
# Remove dangling (untagged) images. Safe to run monthly.
30 3 1 * * docker image prune -f >> /tmp/docker_cleanup.log 2>&1

# --- Disk Space Check (early warning, log only) ---
# Logs current disk usage so you can review trends via /tmp/disk_check.log.
# Alert fires via Prometheus/Alertmanager at <20% free (HostDiskSpaceWarn).
0 6 * * * df -h / >> /tmp/disk_check.log 2>&1

# --- SSD Health Snapshot (requires smartmontools) ---
# Install: sudo apt install smartmontools
# Uncomment after installation. Records SSD health weekly for wear tracking.
# 0 4 * * 0 sudo smartctl -a /dev/sda >> /tmp/ssd_health.log 2>&1
```

## WAL Archive Retention Decision

| Retention | Disk cost/year | Use case |
|-----------|---------------|----------|
| 3 days    | ~93 GB        | Minimal; risk missing replay window |
| **7 days** | **~217 GB**   | **Recommended: covers weekly review cycle** |
| 14 days   | ~434 GB       | Conservative for extended debugging |
| 30 days   | ~930 GB       | Exceeds drive capacity — NOT recommended |

The 7-day default matches the Prometheus retention window (`--storage.tsdb.retention.time=30d`
as of 2026-03-02), so a full week of metrics + WAL data aligns for incident investigation.

## Local Development (make target)

For local cleanup with confirmation prompt:

```bash
# Dry-run: preview files that would be deleted
make wal-archive-cleanup WAL_KEEP_DAYS=7

# Longer retention during active debugging
make wal-archive-cleanup WAL_KEEP_DAYS=14

# Custom archive dir
make wal-archive-cleanup WAL_ARCHIVE_DIR=/data/wal/archive WAL_KEEP_DAYS=7
```

## Verification

After crontab installation, verify the first weekly cleanup ran:

```bash
# Check WAL cleanup log
cat /tmp/wal_cleanup.log

# Check remaining archive files
ls -lh /home/charl/subhft/.wal/archive/ | tail -20

# Check disk usage trend
tail -50 /tmp/disk_check.log
```

## Related Runbooks

- `docs/runbooks/disk-crisis-sop.md` — emergency disk recovery procedure
- `docs/runbooks/recorder-wal-disk-pressure.md` — WAL pressure handling
- `docs/operations/data-retention-policy.md` — full retention policy decisions
