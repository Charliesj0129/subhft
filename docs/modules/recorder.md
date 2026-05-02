# recorder — Durable Storage Pipeline

> **Package**: `src/hft_platform/recorder/`
> **Runtime Plane**: Persistence
> **Files**: 22

## Overview

Durable storage pipeline: columnar double-buffer batching, ClickHouse insert with WAL fallback, WAL-first mode (CE-M3), WAL replay with dedup/DLQ, and disk pressure monitoring.

## Architecture

```
MarketDataService → recorder_queue (bounded, drop on full)
  → RecorderService.run()
  → Batcher.add() → check_flush()
  → DataWriter → ClickHouse INSERT
                → WAL fallback (if CH fails)
```

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `service.py` | `RecorderService` | Main recording loop |
| `batcher.py` | `Batcher` | Columnar double-buffer batching |
| `writer.py` | `DataWriter` | ClickHouse insert with retry |
| `wal.py` | `WALWriter`, `WALReader` | Write-ahead log for durability |
| `wal_loader.py` | `WALLoaderService` | WAL replay with dedup |
| `disk_monitor.py` | `DiskPressureMonitor` | Disk usage monitoring |
| `shard_claim.py` | `ShardClaimManager` | Multi-instance shard coordination |
| + 15 more | — | Record mapping, schema, utilities |

## Recording Path

1. Events arrive on `recorder_queue` (bounded, `put_nowait` with drop policy)
2. `RecorderService` consumes and routes to appropriate `Batcher`
3. `Batcher` accumulates in columnar format (double-buffer for zero-copy swap)
4. Flush triggered by count threshold or time interval
5. `DataWriter` performs ClickHouse batch INSERT
6. On failure → WAL fallback write

### WAL-First Mode (CE-M3)

```
HFT_RECORDER_MODE=wal_first
```

- All writes go to WAL first (no direct ClickHouse)
- Separate WAL loader process replays to ClickHouse
- Higher durability, slightly higher latency

## Batcher

Columnar double-buffer pattern:

```python
batcher = Batcher(table="market_data", flush_size=1000, flush_interval_s=5.0)
batcher.add(record)
if batcher.check_flush():
    batch = batcher.swap()  # Zero-copy buffer swap
    writer.insert(batch)
```

- Double-buffer: write to buffer A while flushing buffer B
- Columnar format: `RustColumnarBuffer` for efficient ClickHouse insert
- Flush triggers: count threshold OR time interval

## WAL (Write-Ahead Log)

```python
writer = WALWriter(path=".wal/")
writer.write(topic, payload)  # Atomic append

loader = WALLoaderService(wal_dir=".wal/", ch_client=client)
await loader.replay()  # Dedup-aware replay
```

- Idempotent replay: dedup keys prevent duplicate inserts
- DLQ for malformed WAL entries
- Retention: `HFT_WAL_RETENTION_DAYS`

## DiskPressureMonitor

- Monitors `.wal/` directory disk usage
- Alerts on low space
- Triggers degraded mode on critical pressure
- Metrics: `wal_disk_available_mb`

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_RECORDER_MODE` | `direct` | `direct` or `wal_first` |
| `HFT_CLICKHOUSE_HOST` | `localhost` | ClickHouse host |
| `HFT_CLICKHOUSE_ENABLED` | — | Enable ClickHouse recording |
| `HFT_WAL_RETENTION_DAYS` | — | WAL file retention |
| `HFT_RECORDER_FLUSH_SIZE` | — | Batch flush threshold |
| `HFT_RECORDER_FLUSH_INTERVAL_S` | — | Batch flush interval |

## Invariants

- Recording MUST NEVER block the hot path (use `put_nowait()` with drop policy)
- ClickHouse failure must not drop data silently (WAL fallback mandatory)
- WAL replay must be idempotent-safe or dedup-aware
