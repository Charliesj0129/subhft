# Ops Rules

## Docker Compose

- Primary deployment: `docker compose up -d --build`
- Service health: `docker compose ps` — all services should show `Up (healthy)`.
- Logs: `docker compose logs -f hft-engine` for trading runtime.
- Reset: `make docker-clean` removes volumes. **THIS DELETES ALL CLICKHOUSE DATA**.

## Key Services and Ports

| Service        | Port                       | Health Check        |
| -------------- | -------------------------- | ------------------- |
| `hft-engine`   | 9090 (Prometheus)          | `/metrics` endpoint |
| `clickhouse`   | 8123 (HTTP), 9000 (native) | `SELECT 1`          |
| `redis`        | 6379                       | `redis-cli ping`    |
| `prometheus`   | 9091                       | `/-/healthy`        |
| `grafana`      | 3000                       | `/api/health`       |
| `alertmanager` | 9093                       | `/-/healthy`        |

## Config Changes Affecting Live Trading

Follow `docs/ops_change_control.md`:

1. Document what/why/risk/rollback.
2. Apply in sim mode first (`HFT_MODE=sim`).
3. Verify metrics for 5 minutes.
4. Rollback: revert config and restart container.

## Common Operations

```bash
# Rebuild after code change
docker compose build hft-engine && docker compose up -d hft-engine

# WAL replay after ClickHouse downtime
docker compose run --rm wal-loader

# Check ClickHouse data
docker exec clickhouse clickhouse-client \
  --query "SELECT count(), max(toDateTime64(exch_ts/1e9,3)) FROM hft.market_data"

# Monitor runtime health
docker compose logs -f hft-monitor
```

## Resource Limits

- `hft-engine`: 2 CPUs, 2GB RAM (configured in docker-compose.yml)
- `clickhouse`: 4GB RAM recommended
- WAL directory (`.wal/`): monitor disk usage. Set `HFT_WAL_RETENTION_DAYS` for auto-cleanup.

## Environment Variables (Ops-Critical)

| Variable                 | Default    | Impact                             |
| ------------------------ | ---------- | ---------------------------------- |
| `HFT_MODE`               | `sim`      | `live` enables real orders         |
| `HFT_CLICKHOUSE_ENABLED` | —          | Enables ClickHouse recording       |
| `HFT_RECORDER_MODE`      | `direct`   | `wal_first` for WAL-only           |
| `HFT_GATEWAY_ENABLED`    | `0`        | Enables CE-M2 gateway pipeline     |
| `HFT_OBS_POLICY`         | `balanced` | `minimal` reduces metrics overhead |
