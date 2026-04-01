---
name: troubleshoot-metrics
description: Use when diagnosing runtime health, Prometheus anomalies, Docker service state, StormGuard transitions, WAL pressure, Redis connectivity, ClickHouse operational issues, multi-broker health, monitor TUI, reconnect diagnostics in the HFT platform.
---

# HFT Diagnostics

Use this skill for live triage and operator-style health checks.

## First Pass

Run the fast health checks first:

```bash
docker compose ps
docker exec redis redis-cli ping
curl -fsS http://localhost:8123/ping
curl -fsS http://localhost:9090/metrics | grep hft_loop_lag
```

Confirm service liveness before chasing deeper application hypotheses.

## Primary Signals

Focus on:

- loop lag
- queue depth
- StormGuard state
- fill activity stalls
- WAL file growth
- disk headroom

Treat StormGuard entering `STORM` or `HALT`, prolonged queue growth, or runaway WAL accumulation as production-severity signals.

## Typical Triage Order

Follow this order:

1. verify container and process health
2. inspect Prometheus metrics for lag, queue, and StormGuard state
3. inspect ClickHouse and Redis health
4. inspect WAL pressure and recorder mode
5. inspect recent service logs
6. apply the relevant runbook if the issue is known

## Useful Commands

```bash
docker compose logs -f hft-engine --tail=100
docker compose logs -f clickhouse --tail=50
docker compose logs -f redis --tail=30
ls -1 .wal/*.wal 2>/dev/null | wc -l
```

## Runbook Boundary

Use the dedicated runbooks when you confirm one of these classes of incidents:

- ClickHouse disk crisis
- recorder recovery after ClickHouse outage
- strategy rollback
- feature-plane shadow or canary operations

Use `clickhouse-io` when the issue becomes a schema, TTL, or analytical-query problem instead of a general health incident.

## Multi-Broker Diagnostics

```bash
# Check active broker
echo $HFT_BROKER  # shioaji or fubon

# Broker-specific metrics
curl -s http://localhost:9090/metrics | grep -E "broker_reconnect|order_latency.*broker|quote_staleness"

# Shioaji session health
curl -s http://localhost:9090/metrics | grep shioaji_session

# Fubon WebSocket health
curl -s http://localhost:9090/metrics | grep fubon_ws
```

## Monitor TUI Diagnostics

Monitor data sources are controlled by `HFT_MONITOR_SOURCE` (values: `clickhouse`, `redis`, `hybrid`).

- Redis live cache check: `docker exec redis redis-cli GET hft:live:tick:2330`
- Monitor config env vars: `HFT_MONITOR_LIVE_ENABLED`, `HFT_MONITOR_REDIS_HOST`, `HFT_MONITOR_REDIS_PORT`
- SSH tunnel for remote access: `ssh -L 6379:localhost:6379 user@remote`

## Reconnect Diagnostics

Key environment variables for reconnect behavior:

- Trading hours window: `HFT_RECONNECT_HOURS` (default `08:30-13:35`)
- Secondary window: `HFT_RECONNECT_HOURS_2`
- Quote flap detection: `HFT_QUOTE_FLAP_THRESHOLD` (default 5), `HFT_QUOTE_FLAP_WINDOW_S` (default 60s), `HFT_QUOTE_FLAP_COOLDOWN_S` (default 300s)
- Feed gap halt: `HFT_STORMGUARD_FEED_GAP_HALT_S` (default 30s)
- Backoff config: `HFT_RECONNECT_BACKOFF_S` (default 5s), `HFT_RECONNECT_BACKOFF_MAX_S` (default 120s)
- Cooldown: `HFT_RECONNECT_COOLDOWN` (default 60s)

## Execution Plane Diagnostics

```bash
# Position drift
curl -s http://localhost:9090/metrics | grep position_drift_qty

# E2E order latency
curl -s http://localhost:9090/metrics | grep e2e_order_latency

# Fill DLQ (orphaned fills)
curl -s http://localhost:9090/metrics | grep fill_dlq

# Circuit breaker state
curl -s http://localhost:9090/metrics | grep circuit_breaker_state

# Execution optimizer decisions
curl -s http://localhost:9090/metrics | grep execution_optimizer

# Slippage
curl -s http://localhost:9090/metrics | grep slippage
```

## Operations Diagnostics

```bash
# Session governor phase
curl -s http://localhost:9090/metrics | grep session_phase

# Autonomy mode
curl -s http://localhost:9090/metrics | grep autonomy_mode

# Recorder bridge drops
curl -s http://localhost:9090/metrics | grep recorder_bridge_drops

# Rust fallback tracking
curl -s http://localhost:9090/metrics | grep rust_fallback_total

# Position checkpoint status
curl -s http://localhost:9090/metrics | grep checkpoint
```

## Pre/Post Market Health

```bash
make pre-market-check     # Docker, CK, Redis, WAL, metrics
make post-market-check    # WAL drained, CK rows, PnL reconciled
make recorder-status      # WAL backlog + CK insert health
```
