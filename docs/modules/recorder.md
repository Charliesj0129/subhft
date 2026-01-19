# recorder

## Purpose
Durable storage for market data, orders, fills, and risk logs.

## Key Files
- `src/hft_platform/recorder/wal.py`: Write-ahead log for durability.
- `src/hft_platform/recorder/batcher.py`: Batch buffering.
- `src/hft_platform/recorder/worker.py`: Worker loop.
- `src/hft_platform/recorder/writer.py`: ClickHouse writer.
- `src/hft_platform/recorder/loader.py`: WAL re-ingestion.
- `src/hft_platform/recorder/mapper.py`: Event-to-table mapping.

## Configuration
- `config/recorder.yaml`
- `HFT_CLICKHOUSE_ENABLED`, `HFT_CLICKHOUSE_HOST`, `HFT_CLICKHOUSE_PORT`.

## Notes
- If ClickHouse is disabled, WAL still records locally.
- Use `loader.py` to backfill after downtime.
