# Runbook: Recorder WAL Disk Pressure

## Scope

This runbook covers the scenario where the WAL disk guard circuit breaker activates due to insufficient free disk space on the WAL volume.

## Key Environment Flags

| Variable | Default | Purpose |
|---|---|---|
| `HFT_WAL_DISK_MIN_MB` | `500` | Minimum free MB before guard activates |
| `HFT_WAL_DISK_PRESSURE_POLICY` | `drop` | `drop` = drop records; `warn` = log warning only |
| `HFT_RECORDER_MODE` | `direct` | `wal_first` routes all writes through WAL first |
| `HFT_WAL_LOADER_CONCURRENCY` | `4` | Threads for parallel WAL replay |

## Metrics

- `hft_wal_files_pending` — current WAL backlog file count
- `hft_wal_disk_free_mb` — free disk space on WAL volume
- `hft_recorder_queue_depth` — in-memory recorder queue depth
- `hft_wal_write_errors_total` — cumulative write errors

## Symptoms

- Log: `wal_disk_pressure_drop` events appearing repeatedly
- Prometheus: `hft_wal_disk_free_mb` drops below `HFT_WAL_DISK_MIN_MB`
- Prometheus: `hft_wal_files_pending` growing without drain
- `hft recorder status` shows `ACTIVE` guard status

## Immediate Response

1. **Check current state:**
   ```bash
   make recorder-status
   df -h /data/wal   # or wherever WAL_DIR is mounted
   ```

2. **If policy=drop** (data loss occurring): escalate immediately.
   Temporarily reduce write rate or halt non-critical data ingestion.

3. **Free up space** (if safe):
   ```bash
   # Confirm WAL files are safe to delete (only after ClickHouse confirms replay complete)
   ls -lh data/wal/
   # Remove fully replayed WAL files ONLY after verifying ClickHouse has them
   ```

4. **Raise guard threshold** as temporary relief:
   ```bash
   export HFT_WAL_DISK_MIN_MB=100  # lower floor while clearing disk
   ```

## Recovery Procedure

1. Ensure ClickHouse is healthy: `make recorder-status` shows `ok`.
2. Allow WAL loader to drain backlog — watch `hft_wal_files_pending` fall to 0.
3. Expand disk volume or archive old data to restore headroom.
4. Reset `HFT_WAL_DISK_MIN_MB` to the standard value for your profile.
5. Verify no data gaps in ClickHouse tick tables.

## Prevention

- Provision disk with ≥10× the expected daily WAL volume.
- Set up Prometheus alert on `hft_wal_disk_free_mb < HFT_WAL_DISK_MIN_MB * 2`.
- Use the `high_throughput` profile only on hosts with NVMe RAID and ≥500 GB free.
- Schedule regular ClickHouse compaction and old-data archival.

## Drill

To test this runbook without real data loss:

```bash
make drill-wal-pressure
```

This runs `hft recorder status` with `HFT_WAL_DISK_MIN_MB=999999` to simulate the guard activation without affecting production state.
