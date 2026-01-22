---
description: Verify market data flow, metrics, and ClickHouse ingestion.
---

# Verify Data Flow

1) Service health
```
docker compose ps
```

2) Metrics reachable
```
curl -s http://localhost:9090/metrics | head -n 5
```

3) LOB updates increasing (replace symbol as needed)
```
curl -s http://localhost:9090/metrics | grep 'lob_updates_total{symbol="TXFB6"'
sleep 2
curl -s http://localhost:9090/metrics | grep 'lob_updates_total{symbol="TXFB6"'
```

4) ClickHouse ingestion
```
docker compose exec -T clickhouse clickhouse-client -q "SELECT count() FROM hft.market_data"
docker compose exec -T clickhouse clickhouse-client -q "SELECT symbol, count() FROM hft.market_data GROUP BY symbol ORDER BY count() DESC LIMIT 10"
```

5) Error counters (should be 0)
```
curl -s http://localhost:9090/metrics | grep -E 'normalization_errors_total|recorder_failures_total|bus_overflow_total|order_reject_total'
```
