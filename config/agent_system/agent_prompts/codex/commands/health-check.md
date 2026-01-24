---
description: Quick system health check (docker, metrics, clickhouse).
---

# Health Check

```
docker compose ps
curl -s http://localhost:9090/metrics | head -n 5
docker compose exec -T clickhouse clickhouse-client -q "SELECT count() FROM hft.market_data"
```
