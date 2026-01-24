---
description: Verify data flow using metrics and ClickHouse queries.
---

# Verify Data Flow

1) docker compose ps
2) curl http://localhost:9090/metrics
3) LOB updates increasing
4) ClickHouse ingestion checks
5) Error counters are zero

Commands
```
docker compose ps
curl -s http://localhost:9090/metrics | head -n 5
curl -s http://localhost:9090/metrics | grep 'lob_updates_total{symbol="TXFB6"'
sleep 2
curl -s http://localhost:9090/metrics | grep 'lob_updates_total{symbol="TXFB6"'
docker compose exec -T clickhouse clickhouse-client -q "SELECT count() FROM hft.market_data"
docker compose exec -T clickhouse clickhouse-client -q "SELECT symbol, count() FROM hft.market_data GROUP BY symbol ORDER BY count() DESC LIMIT 10"
```
