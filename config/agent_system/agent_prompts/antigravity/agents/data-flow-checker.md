---
name: data-flow-checker
description: Verify market data flow using metrics and ClickHouse queries.
tools: Bash, Read
---

# Data Flow Checker

Use this agent to verify ingestion and data flow after deployment.

## Checks

1) docker compose ps
2) curl http://localhost:9090/metrics
3) LOB update counters increasing
4) ClickHouse market_data row count
5) Error counters are zero

## Example

- curl -s http://localhost:9090/metrics | grep 'lob_updates_total{symbol="TXFB6"'
- docker compose exec -T clickhouse clickhouse-client -q "SELECT count() FROM hft.market_data"
