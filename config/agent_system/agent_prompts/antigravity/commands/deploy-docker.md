---
description: Build and run the stack via docker compose.
---

# Deploy Docker

```
make start
```

Then check:
```
docker compose ps
curl -s http://localhost:9090/metrics | head -n 20
```
