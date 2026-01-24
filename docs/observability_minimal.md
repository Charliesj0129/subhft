# Observability Minimal Set

These metrics are required in all environments.

## Latency
- Tick-to-normalized event latency (p50/p95/p99)
- Tick-to-trade latency (p95/p99)

## Reliability
- Message drop count / rate
- Reconnect count
- Heartbeat gap (seconds)

## Pipeline Health
- Raw queue depth
- Recorder queue depth
- Risk queue depth
- Recorder write failures

## Broker
- Order reject count
- Cancel/replace failure count

## Alerts (minimum)
- Latency p99 above threshold for 5m
- Message gap > 15s
- Recorder failures > 0 in 5m
- Queue depth > max size for 1m
