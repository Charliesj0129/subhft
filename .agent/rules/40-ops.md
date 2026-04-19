# Ops Rules

## Docker Compose

- Deploy: `docker compose up -d --build`
- Health: `docker compose ps` — all services should be `Up (healthy)`.
- Logs: `docker compose logs -f hft-engine` for trading runtime.
- Reset: `make docker-clean` removes volumes — **THIS DELETES ALL CLICKHOUSE DATA**.

## Services and Ports

| Service      | Port                       | Health              |
| ------------ | -------------------------- | ------------------- |
| `hft-engine` | 9090 (Prometheus)          | `/metrics`          |
| `clickhouse` | 8123 HTTP / 9000 native    | `SELECT 1`          |
| `redis`      | 6379                       | `redis-cli ping`    |
| `prometheus` | 9091                       | `/-/healthy`        |
| `grafana`    | 3000                       | `/api/health`       |
| `alertmanager` | 9093                     | `/-/healthy`        |

## Config Changes Affecting Live Trading

Follow `docs/ops_change_control.md`:
1. Document what/why/risk/rollback.
2. Apply in sim mode first (`HFT_MODE=sim`).
3. Verify metrics for 5 minutes.
4. Rollback: revert config and restart container.

## Common Operations

```bash
docker compose build hft-engine && docker compose up -d hft-engine   # rebuild
docker compose run --rm wal-loader                                    # WAL replay
docker exec clickhouse clickhouse-client --query \
  "SELECT count(), max(toDateTime64(exch_ts/1e9,3)) FROM hft.market_data"
```

## Resource Limits

- `hft-engine`: 2 CPUs, 2GB RAM (docker-compose.yml).
- `clickhouse`: 4GB RAM recommended.
- WAL dir (`.wal/`): monitor disk; set `HFT_WAL_RETENTION_DAYS` for auto-cleanup.

## Ops-Critical Env Vars

`HFT_MODE` (`sim`|`live`), `HFT_CLICKHOUSE_ENABLED`, `HFT_RECORDER_MODE` (`direct`|`wal_first`), `HFT_GATEWAY_ENABLED`, `HFT_OBS_POLICY` (`balanced`|`minimal`). Full list: see `hft-env-vars` skill.
