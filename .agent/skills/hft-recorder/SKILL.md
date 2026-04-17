<!-- REVIEW-2026-04-17: unreferenced by rules/workflows/teams/agents. Confirm or delete. -->
---
name: hft-recorder
description: Use when working on the persistence pipeline — recorder service, batcher, ClickHouse writer, WAL, WAL-first mode, WAL loader, disk monitor, shard claim, or any code in recorder/.
---

# HFT Recorder (Persistence Pipeline)

Use this skill for `src/hft_platform/recorder/` (22 files). The recorder is the most frequently changed module (10+ changes in 200 commits) due to durability hardening.

## Module Map (22 files)

### Core Pipeline
| File | Class | Purpose |
| --- | --- | --- |
| `worker.py` | `RecorderService` | Main service: topic routing to 7 per-table batchers, schema extractors (CC-5) |
| `batcher.py` | `Batcher`, `ColumnarBuffer`, `GlobalMemoryGuard` | Columnar double-buffer (CC-2), backpressure policy, cross-table memory budget (EC-1) |
| `writer.py` | `DataWriter` | ClickHouse insert + exponential backoff (5 retries, max 30s) |
| `mapper.py` | `map_event_to_record()` | Event -> CH record transformation with instrument support |
| `schema.py` | `apply_schema()` | Migration runner (auto-applied on boot) |
| `mode.py` | `RecorderMode` | DIRECT vs WAL_FIRST mode selection |

### WAL Subsystem
| File | Class | Purpose |
| --- | --- | --- |
| `wal.py` | `WALWriter`, `WALBatchWriter` | JSON lines writer, disk circuit breaker (min 500MB), coalesced multi-table files |
| `wal_first.py` | WAL-only write path | CE-M3: all writes to WAL, async loader replays to CH |
| `wal_scheduler.py` | WAL cleanup scheduler | Auto-archive old WAL files |
| `loader.py` | `WALLoaderService` | Poll + parse + dedup + insert + DLQ (10 retries, 5s-300s backoff) |
| `_loader_batch.py` | Batch formatter | Format WAL rows for bulk insert |
| `_loader_ch.py` | CH connection | Connection management with retry |
| `_loader_dlq.py` | Dead-letter queue | Quarantine corrupt batches |
| `_loader_wal.py` | File processor | Discovery + parsing |
| `_loader_manifest.py` | Processed file tracking | State persistence |
| `_loader_cleanup.py` | Archive cleanup | Delete old processed files |

### Safety & Monitoring
| File | Class | Purpose |
| --- | --- | --- |
| `disk_monitor.py` | `DiskPressureMonitor` | Background daemon: OK -> WARN -> CRITICAL -> HALT levels |
| `shard_claim.py` | `FileClaimRegistry` | fcntl-based exclusive file ownership for multi-loader scale-out |
| `health.py` | `PipelineHealthTracker` | EC-5: INITIALIZING -> HEALTHY -> DEGRADED -> UNHEALTHY |
| `replay_contract.py` | Replay preconditions | Validation before WAL replay |
| `audit.py` | `AuditWriter` | Audit events -> ClickHouse |

## Data Flow

### DIRECT Mode (default: `HFT_RECORDER_MODE=direct`)
```text
Event -> recorder_queue (16384, put_nowait, DROP on full)
  -> RecorderService.run() -> dispatch to batchers[topic]
    -> Batcher.add_event() -> ColumnarBuffer (column_name -> list[values])
      Flush triggers: 100ms elapsed | 10K rows | memory pressure
      Double-buffer swap (CC-2): atomic active<->standby
        -> DataWriter.insert_batch() -> ClickHouse (HTTP 8123)
           On failure -> WALWriter (fallback)
```

### WAL_FIRST Mode (`HFT_RECORDER_MODE=wal_first`)
```text
Same ingestion path, but:
  -> WALWriter.write_batch() -> .wal/*.jsonl (fsync)
    -> WALLoaderService (polling 1s, separate process)
      -> discover files -> parse headers -> check wal_dedup table
        -> insert_with_retry() -> ClickHouse
        -> mark_processed() -> cleanup_old()
        On corrupt: -> DLQ (quarantine)
```

## Schema Extractors (CC-5, hot path)

7 per-table extractors bypass generic serialization:

| Topic | Extractor | Columns |
| --- | --- | --- |
| `market_data` | `_extract_market_data_values()` | 18 columns (symbol, exch_ts, price_scaled, bids/asks arrays...) |
| `orders` | `_extract_order_values()` | 11 columns |
| `fills` | `_extract_fill_values()` | 17 columns (incl. decision_price, arrival_price for TCA) |
| `pnl_snapshots` | `_extract_pnl_snapshot_values()` | 11 columns |
| `risk_log` | generic | variable |
| `latency_spans` | generic | variable |
| `backtest_runs` | generic | variable |

## Common Fix Patterns (from git history)

| Pattern | Fix | Commit type |
| --- | --- | --- |
| Queue drops silently | Add Prometheus metric + log | `fix(persistence): P-01` |
| Unknown topic drops | Log warning on unknown topic | `fix(persistence): P-03` |
| WAL DLQ not crash-safe | Add fsync to DLQ writes | `fix(persistence): P-06` |
| Fill extractor wrong field | Correct field name lookup for FillEvent | `fix(persistence): P-08` |
| Async flush failure loses data | Recover data on WALBatchWriter failure | `fix(persistence): P-20` |
| Batcher circuit breaker drops | Add Prometheus metric | `fix(persistence): P-21` |
| Shutdown data loss | Drain queue on shutdown | `fix(recorder)` |

## Critical Rules

1. **NEVER block hot path**: `put_nowait()` with drop policy. Recording failure must not stall trading.
2. **WAL replay must be idempotent**: use `hft.wal_dedup` table for content-hash dedup.
3. **Schema changes must preserve replay**: additive columns OK, renames break WAL replay.
4. **Disk circuit breaker**: WALWriter refuses writes below 500MB free disk.
5. **GlobalMemoryGuard**: drops low-priority batchers when total memory exceeds budget.
6. **Shard claim**: fcntl exclusive lock per WAL file for multi-loader safety.

## Environment Variables

| Variable | Default | Effect |
| --- | --- | --- |
| `HFT_RECORDER_MODE` | `direct` | `wal_first` = WAL-only path (CE-M3) |
| `HFT_CLICKHOUSE_HOST` | `localhost` | ClickHouse connection |
| `HFT_CLICKHOUSE_PORT` | `8123` | HTTP port (clickhouse-connect) |
| `HFT_WAL_SHARD_CLAIM_ENABLED` | `0` | Multi-loader shard claim |
| `HFT_ARCHIVE_RETENTION_DAYS` | `3` | WAL archive retention |

## Operational Commands

```bash
make recorder-status           # WAL backlog + CH status
make wal-dlq-status            # DLQ count/bytes/age
make wal-dlq-replay-dry-run    # Preview DLQ replay
make wal-dlq-replay            # Replay DLQ to CH (live)
make wal-archive-cleanup       # Clean old archives
make wal-manifest-tmp-cleanup  # Clean orphan temp files
make drill-ck-down             # 30s CH outage drill
make drill-wal-pressure        # Disk pressure drill
```

## Testing

```bash
make test-file FILE=tests/unit/test_recorder_worker.py
make test-file FILE=tests/unit/test_batcher_emergency_wal.py
make test-clickhouse-writer-smoke
make verify-ce3                 # WAL hardening integration tests
```
