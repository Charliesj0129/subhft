# HFT Platform

High-performance event-driven trading platform with Shioaji integration, ClickHouse recorder, and HftBacktest support.

## Quick Start (Sim, Local)
```bash
# 1) Install deps
uv sync --dev

# 2) Create env file
cp .env.example .env

# 3) Build symbols.yaml from symbols.list
uv run hft config build --list config/symbols.list --output config/symbols.yaml

# 4) Run in simulation mode
uv run hft run sim

# 5) Verify metrics
# http://localhost:9090/metrics
```

> Tip: If the `hft` command is not on PATH, use `uv run hft ...` or `python -m hft_platform ...`.

## Full Stack (Docker Compose - Default)
```bash
docker compose up -d --build
docker compose logs -f hft-engine
```

- Prometheus: http://localhost:9091
- Grafana: http://localhost:3000 (admin / admin by default)
- Alertmanager: http://localhost:9093

Stop compose stack:
```bash
docker compose down
```

## Docker Swarm (Optional)
```bash
docker swarm init 2>/dev/null || true
docker build -t ${HFT_IMAGE:-hft-platform:latest} .
docker stack deploy -c docker-stack.yml hft
docker service logs -f hft_hft-engine
docker stack rm hft
```

## Live Trading (Explicit Only)
```bash
export SHIOAJI_API_KEY=... 
export SHIOAJI_SECRET_KEY=...
export HFT_MODE=live

# Optional CA (for order signing)
export SHIOAJI_PERSON_ID=...
export SHIOAJI_CA_PATH=/path/to/Sinopac.pfx
export SHIOAJI_CA_PASSWORD=...

uv run hft run live
```
If credentials are missing, the CLI auto-downgrades to `sim` and prints a warning.

## Docs
Start here:
- `docs/README.md` (index)
- `docs/getting_started.md` (full, step-by-step usage)
- `docs/quickstart.md` (10-minute path)
- `docs/cli_reference.md` / `docs/config_reference.md`

## Project Map (Top Level)
- `src/hft_platform/`: Core runtime (services, strategy, risk, execution, recorder).
- `config/`: Config files and environment overlays.
- `docs/`: User + ops documentation.
- `scripts/`: Utility scripts (latency probes, pipeline validation, snapshots).
- `rust_core/`: Rust extension module.
- `tests/`: Unit + integration tests.

## Testing
```bash
uv run ruff check --fix
uv run pytest
```

## Safety + HFT Laws (short)
- No heap allocations on the hot path.
- Structure-of-Arrays for locality.
- No blocking I/O on the event loop.
- Never use float for prices/balances/PnL (use scaled int or Decimal).
- Python <-> Rust must be zero-copy.

---
For detailed workflows (symbols, strategy, latency measurement, deployment), read `docs/getting_started.md`.
