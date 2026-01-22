---
description: Deploy the stack with docker compose and validate metrics.
---

# Deploy with Docker

1) Build and start
```
make start
```

2) Check health
```
docker compose ps
```

3) Validate metrics
```
curl -s http://localhost:9090/metrics | head -n 20
```

Notes:
- Use SYMBOLS_CONFIG=config/symbols.yaml for expanded symbols.
- SHIOAJI credentials must be in .env for live mode.
