# Quickstart (10 Minutes)

This is the shortest path to get a working simulation + metrics endpoint.
For the full guide, read `docs/getting_started.md`.

## 1) Install deps
```bash
uv sync --dev
```

## 2) Create `.env`
```bash
cp .env.example .env
```

## 3) Build `symbols.yaml`
```bash
uv run hft config build --list config/symbols.list --output config/symbols.yaml
```

## 4) Run (simulation)
```bash
uv run hft run sim
```

## 5) Verify
- Metrics: http://localhost:9090/metrics
- Logs: console output (structlog JSON)

## Optional: Full stack with Docker
```bash
docker compose up -d --build

docker compose logs -f hft-engine
```

- Prometheus UI: http://localhost:9091
- Grafana: http://localhost:3000

---
Next: `docs/getting_started.md` for symbols, strategy, backtest, latency measurement.
