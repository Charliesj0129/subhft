---
name: hft-recorder
description: "Persistence pipeline: RecorderService, Batcher, DataWriter, WAL and WAL-first mode, loader, DLQ, disk monitor, replay idempotence. Use when editing recorder/ or debugging dropped, duplicate, or missing rows and WAL backlog. Not for ClickHouse schema or queries (clickhouse-io)."
---

# Recorder (persistence)

Off the hot path but a durability path: it must never block the loop, and
replay must stay idempotent. Laws are in `AGENTS.md`.

```
event -> recorder_queue (bounded, put_nowait, drop on full + metric)
  -> RecorderService (worker.py) -> per-topic Batcher (columnar double buffer; flush on age/rows/memory)
  DIRECT   (HFT_RECORDER_MODE=direct):   DataWriter -> ClickHouse; on failure -> WAL
  WAL_FIRST (HFT_RECORDER_MODE=wal_first): WALWriter -> .wal/*.jsonl (fsync)
        -> WALLoaderService (separate process; poll, dedup via hft.wal_dedup, insert, DLQ on corrupt)
```

## Files (`src/hft_platform/recorder/`)

`worker.py` service and per-table extractors; `batcher.py` (`Batcher`,
`GlobalMemoryGuard`); `writer.py` (insert with backoff); `mapper.py`;
`schema.py` (applies migrations on boot); `mode.py`; `wal.py`, `wal_first.py`,
`wal_scheduler.py`; `loader.py` plus `_loader_*.py`; `disk_monitor.py`
(OK -> WARN -> CRITICAL -> HALT); `shard_claim.py` (fcntl claim per WAL file);
`health.py`; `replay_contract.py`; `audit.py`.

## Rules that bite

1. Never block the hot path. A drop needs a metric and a log; unknown topics are logged.
2. WAL replay is idempotent: dedup by content hash in `hft.wal_dedup`. Replay
   must not double rows (use `dedup.py` semantics for duplicate deliveries).
3. Schema changes keep replay working: add columns, never rename. A table
   rebuild (CREATE + RENAME) must carry every later ALTER; a rebuild once left
   `hft.fills` empty for months.
4. WALWriter refuses writes below a free-disk floor; the DLQ must fsync;
   DLQ files are quarantined, not deleted; every `_add_to_dlq` path needs a release.
5. A batch flush failure must recover its data to WAL; shutdown drains the queue.
6. Age comparisons use the monotonic clock, never `now() - field`.
7. Fill extractors read the real FillEvent field names (`decision_price`,
   `arrival_price` for TCA); check the extractor when a column is empty.

## Operate and verify

```bash
make recorder-status            # WAL backlog + ClickHouse status
make wal-dlq-status             # DLQ count/bytes/age
make wal-dlq-replay-dry-run     # preview; `make wal-dlq-replay` writes (ask first on production)
make drill-ck-down              # 30 s ClickHouse outage drill
make drill-wal-pressure
make test-file FILE=tests/unit/test_recorder_worker.py
make test-file FILE=tests/unit/test_batcher_emergency_wal.py
make test-clickhouse-writer-smoke
make verify-ce3                 # WAL hardening integration tests
```

## Done when

Tests above pass, a ClickHouse-down case still ends with the data in WAL,
replay twice yields the same rows, and any new drop path has a metric.
