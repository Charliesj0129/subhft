# Log Search Guide (Loki + LogQL)

## Overview

Platform logs are collected by Promtail and stored in Loki.
Logs use JSON structlog format with consistent field names.
Query via Grafana Explore or `logcli`.

---

## Connection

### Grafana Explore

Navigate to: `http://localhost:3000/explore` and select the Loki data source.

### logcli (CLI)

```bash
export LOKI_ADDR=http://localhost:3100
logcli query '{job="hft-engine"}' --limit=50 --since=1h
```

---

## Common Queries

### 1. All Errors (Last Hour)

```logql
{job="hft-engine"} |= `"level":"error"` | json | line_format "{{.ts}} [{{.level}}] {{.event}} {{.error}}"
```

### 2. Critical Errors Only

```logql
{job="hft-engine"} |= `"level":"critical"` | json
```

### 3. Errors by Module

```logql
{job="hft-engine"} |= `"level":"error"` | json | module = "risk"
```

---

## Order Flow

### All Orders for a Symbol

```logql
{job="hft-engine"} |= `order` |= `"symbol":"2330"` | json | line_format "{{.ts}} {{.event}} side={{.side}} qty={{.qty}} price={{.price}}"
```

### Order Rejections

```logql
{job="hft-engine"} |= `order_rejected` | json | line_format "{{.ts}} {{.strategy}} {{.symbol}} reason={{.reason}}"
```

### Fill Events

```logql
{job="hft-engine"} |= `fill_event` | json | line_format "{{.ts}} {{.symbol}} side={{.side}} qty={{.qty}} price={{.price}} fee={{.fee}}"
```

### Order Lifecycle (by idempotency key)

```logql
{job="hft-engine"} |= `abc123-idempotency-key` | json
```

---

## HALT Events

### StormGuard State Changes

```logql
{job="hft-engine"} |= `storm_guard` | json | line_format "{{.ts}} state={{.state}} trigger={{.trigger_reason}}"
```

### HALT Trigger Details

```logql
{job="hft-engine"} |= `HALT` | json
```

### Recovery Events

```logql
{job="hft-engine"} |= `storm_guard` |= `NORMAL` | json
```

---

## Feed Issues

### Feed Gaps (Stale Data)

```logql
{job="hft-engine"} |= `feed_gap` | json | line_format "{{.ts}} {{.symbol}} gap_ms={{.gap_ms}}"
```

### Feed Disconnections

```logql
{job="hft-engine"} |= `feed` |~ `disconnect|reconnect|timeout` | json
```

### Quote Callback Errors

```logql
{job="hft-engine"} |= `quote_callback` |= `error` | json
```

### Feed Rate Drop

```logql
{job="hft-engine"} |= `feed_rate` | json | line_format "{{.ts}} ticks_per_sec={{.rate}}"
```

---

## Recorder Failures

### ClickHouse Write Errors

```logql
{job="hft-engine"} |= `recorder` |= `error` | json | line_format "{{.ts}} {{.event}} error={{.error}}"
```

### WAL Fallback Activations

```logql
{job="hft-engine"} |= `wal_fallback` | json
```

### Batcher Flush Failures

```logql
{job="hft-engine"} |= `batcher` |= `flush` |= `error` | json
```

### Queue Drop Events

```logql
{job="hft-engine"} |= `recorder_queue_full` | json | line_format "{{.ts}} dropped={{.dropped_count}}"
```

---

## Risk Engine

### Risk Rejections

```logql
{job="hft-engine"} |= `risk_reject` | json | line_format "{{.ts}} {{.strategy}} {{.symbol}} reason={{.reason}}"
```

### Exposure Limit Events

```logql
{job="hft-engine"} |= `exposure` |~ `limit|exceeded|evict` | json
```

---

## Performance

### Slow Ticks (Processing > 1ms)

```logql
{job="hft-engine"} |= `slow_tick` | json | line_format "{{.ts}} {{.symbol}} latency_us={{.latency_us}}"
```

### Queue Depth Warnings

```logql
{job="hft-engine"} |= `queue_depth` |= `warning` | json
```

---

## Aggregation Queries

### Error Count by Module (Last Hour)

```logql
sum by (module) (count_over_time({job="hft-engine"} |= `"level":"error"` | json [1h]))
```

### Order Count by Strategy (Last Hour)

```logql
sum by (strategy) (count_over_time({job="hft-engine"} |= `order_intent` | json [1h]))
```

### HALT Events per Day

```logql
sum(count_over_time({job="hft-engine"} |= `storm_guard` |= `HALT` | json [24h]))
```

---

## Tips

- Use `|=` for exact substring match (faster than regex).
- Use `|~` for regex match (slower, use sparingly).
- Add `| json` after the stream selector to parse structured fields.
- Use `line_format` to create readable output from JSON fields.
- Time ranges: `[5m]`, `[1h]`, `[24h]` for aggregations.
- For high-volume queries, add label matchers first to reduce scan scope.
