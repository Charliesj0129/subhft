# monitor — Live Signal Monitoring TUI

> **Package**: `src/hft_platform/monitor/`
> **Runtime Plane**: Observability
> **Files**: 19

## Overview

Terminal UI for live signal monitoring with dual data sources: ClickHouse (historical) and Redis (live). Supports SSH tunnel for remote monitoring.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `engine.py` | `MonitorEngine` | Data orchestration and polling |
| `renderer.py` | `MonitorRenderer` | TUI rendering engine |
| `tui.py` | `TUI` | Terminal UI framework |
| `ch_poller.py` | `CHPoller` | ClickHouse data polling |
| `redis_poller.py` | `RedisPoller` | Redis live data polling |
| + 14 more | — | Panels, formatters, utilities |

## Panels

| Panel | Data |
|-------|------|
| Portfolio | Positions, PnL, equity |
| Greeks | Options Greeks exposure |
| Orders | Active orders, fill history |
| Positions | Per-symbol position detail |
| Health | System health metrics |
| PnL | Realized/unrealized PnL breakdown |

## Data Sources

| Source | Env | Purpose |
|--------|-----|---------|
| ClickHouse | `HFT_MONITOR_SOURCE=clickhouse` | Historical data (default) |
| Redis | `HFT_MONITOR_SOURCE=redis` | Live data cache |
| Hybrid | `HFT_MONITOR_SOURCE=hybrid` | Both sources |
| SHM | `HFT_MONITOR_DATA_SOURCE=shm` | Shared memory (lowest latency) |

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_MONITOR_SOURCE` | `clickhouse` | Data source |
| `HFT_MONITOR_LIVE_ENABLED` | `0` | Enable Redis live publisher |
| `HFT_MONITOR_REDIS_HOST` | `localhost` | Redis host |
| `HFT_MONITOR_REDIS_PORT` | `6379` | Redis port |
| `HFT_MONITOR_DATA_SOURCE` | `auto` | Data layer: `ch`/`shm`/`auto` |
