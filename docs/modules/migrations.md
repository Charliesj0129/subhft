# migrations — ClickHouse DDL Management

> **Package**: `src/hft_platform/migrations/`
> **Runtime Plane**: Persistence

## Overview

Sequential ClickHouse DDL management with 15 SQL migration files (619 lines). Auto-applied on boot.

## Migration Files

Located in `src/hft_platform/migrations/clickhouse/`:

| Table | TTL | Purpose |
|-------|-----|---------|
| `market_data` | 6 months | Tick and bid/ask events |
| `orders` | 1 year | Order lifecycle events |
| `fills` | 1 year | Trade execution records |
| `audit` | 2 years | Audit trail |
| `config_snapshots` | — | Startup config capture |

## Usage

Migrations auto-apply on boot via `SystemBootstrapper`. Manual replay:

```bash
docker compose run --rm wal-loader  # Replay WAL after CH downtime
```

## Schema Rules

- Runtime schema source of truth: `src/hft_platform/migrations/clickhouse/`
- Schema changes must preserve replay compatibility for existing WAL files
- Legacy SQL files are non-bootstrap references
