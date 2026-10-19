# Runbook: Recorder WAL Disk Pressure

## Scope

This runbook covers the scenario where the WAL disk guard circuit breaker activates due to insufficient free disk space on the WAL volume.

## Key Environment Flags

| Variable | Default | Purpose |
|---|---|---|
| `HFT_WAL_DISK_MIN_MB` | `500` | Minimum free MB before guard activates |
| `HFT_WAL_DISK_PRESSURE_POLICY` | `halt` | Below `HFT_WAL_DISK_MIN_MB` the circuit breaker drops the batch and logs a warning. Only `raise` raises; `halt`, `drop`, `warn` and any other value all drop (`recorder/wal.py:52`, `:97-117`). `halt` by itself does not trigger a HALT, but this is the fallback after a ClickHouse write failure: the dropped batch is recorded as `data_loss` and the recorder goes to `DATA_LOSS` (`recorder/writer.py:723-731`; consequences in `docs/operations/env-vars-reference.md` §6.6). |
| `HFT_WAL_FIRST_POLICY_<TABLE>` | `write` | `wal_first` mode only: at CRITICAL pressure `write` keeps writing, `drop` and `halt` both drop the rows; at HALT pressure every table drops (`recorder/wal_first.py:36-65`) |
| `HFT_RECORDER_MODE` | `direct` | `wal_first` routes all writes through WAL first |
| `HFT_WAL_LOADER_CONCURRENCY` | `4` | Threads for parallel WAL replay |

## Metrics

The names below are exported without an `hft_` prefix (`HFT_METRICS_PREFIX` is empty by default and the compose stack does not set it), so a `grep hft_wal_...` finds nothing; a few other metrics carry `hft_` in their own name (`hft_quote_conn_*`, `hft_backup_*`). `wal_backlog_files` is read with `job="wal-loader"` (the engine job serves a constant-0 copy; see `config/monitoring/prometheus.yml`). Engine-side gauges such as `wal_disk_available_mb` are read with `job="hft-engine"`: the `wal-loader` scrape job keeps every `wal_*` name, so a query without a job matcher can return the loader's series instead.

- `wal_backlog_files` — WAL files pending replay (`job="wal-loader"`)
- `wal_disk_available_mb{job="hft-engine"}` — free disk space on the WAL volume
- `wal_disk_circuit_breaker_active{writer}` — 1 while the disk circuit breaker is dropping batches
- `queue_depth{queue="recorder"}` — in-memory recorder queue depth
- `recorder_wal_skipped_rows_total{writer,table,reason}` — rows skipped under the disk-pressure policy

There is no cumulative WAL write-error counter on this base. `recorder_failures_total` counts WAL-first writes that returned failure (`recorder/worker.py:941`), so read it as a data-loss signal, not as a disk-write error rate.

## Symptoms

- Log: `wal_disk_pressure_drop` events appearing repeatedly
- Prometheus: `wal_disk_available_mb{job="hft-engine"}` drops below `HFT_WAL_DISK_MIN_MB`, or `wal_disk_circuit_breaker_active` is 1
- Prometheus: `wal_backlog_files{job="wal-loader"}` growing without drain
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
2. Allow WAL loader to drain backlog — watch `wal_backlog_files{job="wal-loader"}` fall to 0.
3. Expand disk volume or archive old data to restore headroom.
4. Reset `HFT_WAL_DISK_MIN_MB` to the standard value for your profile.
5. Verify no data gaps in ClickHouse tick tables.

## Prevention

- Provision disk with ≥10× the expected daily WAL volume.
- Set up Prometheus alert on `wal_disk_available_mb{job="hft-engine"} < HFT_WAL_DISK_MIN_MB * 2` (keep the job matcher: without it the `wal-loader` job's series is matched too).
- Use the `high_throughput` profile only on hosts with NVMe RAID and ≥500 GB free.
- Schedule regular ClickHouse compaction and old-data archival.

## Drill

To test this runbook without real data loss:

```bash
make drill-wal-pressure
```

This runs `hft recorder status` with `HFT_WAL_DISK_MIN_MB=999999` to simulate the guard activation without affecting production state.
