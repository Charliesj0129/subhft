# recorder

## Purpose

Durable storage pipeline for market data, orders, fills, risk logs, backtest runs, and latency spans.

## Key Files

- `src/hft_platform/recorder/worker.py`: `RecorderService` — main loop with 6 batchers (296 lines).
- `src/hft_platform/recorder/batcher.py`: `Batcher` — batch buffering with memory guard (19KB).
- `src/hft_platform/recorder/writer.py`: `DataWriter` — ClickHouse INSERT via thread pool (27KB).
- `src/hft_platform/recorder/wal.py`: `WALBatchWriter` — write-ahead log for durability (15KB).
- `src/hft_platform/recorder/wal_first.py`: `WALFirstWriter` — WAL-only mode (CE-M3).
- `src/hft_platform/recorder/loader.py`: `WALLoaderService` — WAL→ClickHouse replay (55KB).
- `src/hft_platform/recorder/mapper.py`: Event-to-table column mapping.
- `src/hft_platform/recorder/mode.py`: `RecorderMode` enum (DIRECT / WAL_FIRST).
- `src/hft_platform/recorder/health.py`: `PipelineHealthTracker`.
- `src/hft_platform/recorder/disk_monitor.py`: `DiskPressureMonitor` for WAL dir.

## Batcher Topics → ClickHouse Tables

| Topic           | Table               | Schema Extractor           |
| --------------- | ------------------- | -------------------------- |
| `market_data`   | `hft.market_data`   | `_extract_market_data()` ✓ |
| `orders`        | `hft.orders`        | `_extract_order()` ✓       |
| `fills`         | `hft.trades`        | `_extract_fill()` ✓        |
| `risk_log`      | `hft.logs`          | generic serialize          |
| `backtest_runs` | `hft.backtest_runs` | generic serialize          |
| `latency_spans` | `hft.latency_spans` | generic serialize          |

## Recorder Modes

| Mode               | Env                           | Behavior                                             |
| ------------------ | ----------------------------- | ---------------------------------------------------- |
| `direct` (default) | `HFT_RECORDER_MODE=direct`    | Batcher → ClickHouse INSERT                          |
| `wal_first`        | `HFT_RECORDER_MODE=wal_first` | Batcher → WAL file only, replay later via wal-loader |

## WAL Recovery

- On startup (direct mode), `recover_wal()` replays `.wal/` files to ClickHouse.
- Skipped in WAL-first mode.
- Standalone: `docker compose run --rm wal-loader`.

## Memory Guard

- `GlobalMemoryGuard` tracks total buffered rows across ALL batchers.
- Prevents OOM by triggering forced flush when threshold exceeded.
- Health tracker records `data_loss` events for monitoring.

## Configuration

| Variable                     | Default     | Purpose                              |
| ---------------------------- | ----------- | ------------------------------------ |
| `HFT_CLICKHOUSE_ENABLED`     | —           | Enable ClickHouse recording          |
| `HFT_CLICKHOUSE_HOST`        | `localhost` | ClickHouse host                      |
| `HFT_CLICKHOUSE_PORT`        | `9000`      | ClickHouse native port               |
| `HFT_RECORDER_MODE`          | `direct`    | Recording mode                       |
| `HFT_WAL_DIR`                | `.wal`      | WAL directory                        |
| `HFT_BATCHER_SCHEMA_EXTRACT` | `1`         | Enable fast schema extractors (CC-5) |
| `HFT_DISABLE_CLICKHOUSE`     | —           | Force disable ClickHouse             |

## Notes

- If ClickHouse is disabled, WAL still records locally.
- Schema extractors (CC-5) bypass generic `serialize()` for ~3x faster extraction.
- Use `loader.py` to backfill after downtime.
