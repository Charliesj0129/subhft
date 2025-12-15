# Observability Contracts

## Metrics
Implemented using Prometheus Client.

| Name | Type | Labels | Description |
|---|---|---|---|
| `feed_events_total` | Counter | `type` (tick, bidask) | Total market data events received |
| `feed_latency_ns` | Histogram | - | Ingest latency (ingest_ts - exchange_ts) |
| `strategy_latency_ns` | Histogram | `strategy` | Execution time for strategy logic |
| `risk_reject_total` | Counter | `reason`, `strategy` | Orders rejected by risk engine |
| `order_actions_total` | Counter | `type` (new, amend, cancel) | Orders sent to broker |
| `execution_events_total` | Counter | `type` (order, fill) | Broker callbacks processed |
| `recorder_failures_total` | Counter | - | Failed writes to ClickHouse |

## Logging Schema
Base fields present in all logs:
```json
{
  "ts": "ISO8601",
  "level": "INFO",
  "service": "hft_platform",
  "component": "...", 
  "event": "message",
  "correlation_id": "...",
  "env": "prod"
}
```

Specific Contexts:
- **Order**: `strategy_id`, `symbol`, `price`, `qty`, `side`, `intent_id`
- **Risk**: `check_name`, `threshold`, `value`
