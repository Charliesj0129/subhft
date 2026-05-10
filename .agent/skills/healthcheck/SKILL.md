---
name: hft-healthcheck
description: Verify HFT platform health across all services and components. Use for routine checks, post-deployment validation, pre-market open verification, and incident response.
---

# HFT Platform Health Check

## When to Use

- Routine health verification
- After deployment or service restart
- Before market open (T-5 min)
- During incident response
- After infrastructure changes (network, disk, config)

## Quick Health Check

Run all checks at once:

```bash
bash .agent/hooks/verify_health.sh all
```

Or check individual components:

### Docker Services

```bash
docker compose ps
# All services should show Up (healthy)
```

### Prometheus (Metrics Export)

```bash
curl -sf http://localhost:9090/metrics > /dev/null && echo "OK" || echo "FAIL"
```

### ClickHouse (Persistence)

```bash
docker exec clickhouse clickhouse-client --query "SELECT 1"
# Expected: 1
```

### Redis (Cache/Live Data)

```bash
docker exec redis redis-cli ping
# Expected: PONG
```

### Grafana (Dashboards)

```bash
curl -sf http://localhost:3000/api/health | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])"
# Expected: ok
```

### Alertmanager

```bash
curl -sf http://localhost:9093/-/healthy > /dev/null && echo "OK" || echo "FAIL"
```

## SLO Targets

Reference: `docs/operations/slo-definitions.md`

| SLO | Target | Measurement |
|-----|--------|-------------|
| Platform Availability | 99.5% | Uptime during trading hours |
| Order-to-Fill P95 | < 50ms | End-to-end latency (includes broker RTT) |
| Data Integrity | 99.9% | No silent data drops in recorder path |
| Feed Reconnect | < 60s | Time to recover from feed disconnect |

## Component-Specific Checks

### Feed Adapter

```bash
curl -s http://localhost:9090/metrics | grep -E "feed_events_total|feed_reconnect"
```

- `feed_events_total` should increase during market hours
- `feed_reconnect_total` should be 0 (non-zero indicates connection instability)

### Recorder

```bash
curl -s http://localhost:9090/metrics | grep -E "recorder_queue_depth|recorder_drops_total|recorder_flush"
```

- `recorder_drops_total` must be 0 in normal operation
- `recorder_queue_depth` should stay below half of max capacity

### Strategy Runner

```bash
curl -s http://localhost:9090/metrics | grep -E "strategy_latency_ns|strategy_intents_total"
```

- Latency should be < 100us for most strategies
- Intents total confirms strategy is active

### Risk Engine

```bash
curl -s http://localhost:9090/metrics | grep -E "risk_check_latency|risk_rejections_total"
```

- Check latency < 50us
- Monitor rejection rate for anomalies

### Gateway (if enabled)

```bash
curl -s http://localhost:9090/metrics | grep -E "gateway_intent_channel_depth|gateway_latency"
```

- Only relevant when `HFT_GATEWAY_ENABLED=1`

## StormGuard FSM States

StormGuard protects the system from adverse market conditions:

| State | Meaning | Action |
|-------|---------|--------|
| NORMAL | All systems healthy | Normal trading |
| WARM | Elevated risk detected | Reduce position sizes, tighten stops |
| STORM | High volatility or anomaly | Halt new orders, allow cancels only |
| HALT | Critical failure (feed gap > 30s, etc.) | All trading stopped, cancels only, manual intervention required |

Recovery path: HALT -> STORM -> WARM -> NORMAL (requires metrics stabilization at each stage).

Check current state:

```bash
curl -s http://localhost:9090/metrics | grep storm_guard_state
```

## Pre-Market Open Checklist (T-5 min)

Run this sequence before market hours (08:30 TWSE):

1. All Docker services healthy: `docker compose ps`
2. Broker session active: check feed adapter logs for successful login
3. Symbols subscribed: `curl -s http://localhost:9090/metrics | grep subscribed_symbols`
4. ClickHouse writable: `docker exec clickhouse clickhouse-client -q "SELECT 1"`
5. WAL directory accessible: `ls -la .wal/`
6. StormGuard in NORMAL: `curl -s http://localhost:9090/metrics | grep storm_guard_state`
7. No pending WAL replay: `ls -1 .wal/*.wal 2>/dev/null | wc -l` (should be 0)
8. Disk space adequate: `df -h /` (> 5GB free)
9. Recent config correct: verify `HFT_MODE` and `HFT_BROKER` match intention

## Incident Response Quick Reference

| Symptom | First Check | Escalation |
|---------|------------|------------|
| No metrics | `docker compose ps` -- is hft-engine running? | Check engine logs |
| Feed stopped | `feed_events_total` stale | Check broker session, reconnect |
| ClickHouse timeout | `docker exec clickhouse ...` | Check disk, RAM, restart CH |
| High queue depth | Identify slowest pipeline stage | Scale or degrade gracefully |
| StormGuard HALT | Check `HFT_STORMGUARD_FEED_GAP_HALT_S` | Verify feed, manual reset if needed |
