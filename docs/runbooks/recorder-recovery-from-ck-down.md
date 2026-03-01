# Runbook: Recorder Recovery from ClickHouse Downtime

## Scope

This runbook covers recovering tick and fill data persistence when ClickHouse becomes unavailable and the WAL fallback accumulates a backlog.

## Key Environment Flags

| Variable | Default | Purpose |
|---|---|---|
| `HFT_RECORDER_MODE` | `direct` | `wal_first` buffers all writes to WAL before CK |
| `HFT_WAL_BATCH_MAX_ROWS` | `500` | Rows per WAL replay batch |
| `HFT_WAL_LOADER_CONCURRENCY` | `4` | Parallel WAL replay threads |
| `HFT_WAL_DEDUP_ENABLED` | `0` | `1` = SHA256 dedup guard during replay |
| `HFT_WAL_STRICT_ORDER` | `0` | `1` = enforce timestamp ordering on replay |

## Metrics

- `hft_wal_files_pending` — WAL backlog file count
- `hft_clickhouse_write_errors_total` — cumulative CK write errors
- `hft_recorder_queue_depth` — in-memory queue depth
- `hft_clickhouse_write_latency_ms` — CK write latency histogram

## Symptoms

- Log: `clickhouse_write_failed` or `recorder_queue_degraded` events
- Prometheus: `hft_clickhouse_write_errors_total` rising rapidly
- `hft recorder status` shows ClickHouse `unreachable`
- `hft_wal_files_pending` accumulating

## Immediate Response

1. **Verify ClickHouse is actually down:**
   ```bash
   make recorder-status
   curl http://localhost:8123/ping
   ```

2. **Check docker / systemd status:**
   ```bash
   docker compose ps clickhouse
   docker compose logs --tail=50 clickhouse
   ```

3. **The WAL fallback is automatic** — no action needed to preserve data while CK is down.
   Confirm the WAL is accumulating:
   ```bash
   ls -lh data/wal/
   ```

4. **Do NOT restart the HFT engine** — it will lose in-memory queue state. Let WAL absorb writes.

## Recovery (After ClickHouse Restored)

1. **Restart ClickHouse:**
   ```bash
   docker compose start clickhouse
   # or: systemctl start clickhouse-server
   ```

2. **Wait for WAL drain** — the loader thread replays WAL automatically once CK is reachable:
   ```bash
   watch -n5 make recorder-status
   ```
   `hft_wal_files_pending` should trend to 0.

3. **Enable dedup guard** if the same events may be replayed twice (e.g., after a restart):
   ```bash
   export HFT_WAL_DEDUP_ENABLED=1
   ```

4. **Verify data integrity:**
   ```bash
   # Check row counts for today in ClickHouse
   curl "http://localhost:8123/?query=SELECT+count()+FROM+hft.ticks+WHERE+toDate(ts_ns/1e9)=today()"
   ```

5. **Clear alert and document** the outage duration and any data gaps found.

## Drill

To simulate and practice this runbook in a safe environment:

```bash
make drill-ck-down
```

This stops ClickHouse for 30 seconds, then restarts it — allowing you to observe WAL accumulation and automatic drain recovery. Run `make recorder-status` during and after the drill.
