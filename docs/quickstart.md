# HFT Platform Quickstart

## 1. Installation
The project uses `uv` for dependency management.

```bash
# Clone
git clone <repo>
cd hft_platform

# Install dependencies (dev)
uv sync --dev
# Or runtime only
uv sync --no-dev
```

## 2. Configuration
Core configuration lives in `config/`.
*   `symbols.list`: Single source for trade/subscribe instruments.
*   `symbols.yaml`: Generated from `symbols.list` (do not hand-edit).
*   `config/base/strategies.yaml` (defaults) and `config/strategies.yaml` (local overrides): Active strategies and parameters.
*   `strategy_limits.yaml`: Risk limits (Max Position, Max Order Size).

Environment variables live in `.env` (copy from `.env.example`):
```bash
cp .env.example .env
```
Secrets must stay in environment variables, never in code.

**Example `symbols.list`**:
```
2330 exchange=TSE tags=stocks
TXF@front exchange=FUT tags=futures|front_month
```

Build the YAML:
```bash
make symbols
```
If you use rule-based entries, run `make sync-symbols` first to refresh the contract cache.

**Batch selection shortcuts**
- `python -m hft_platform config preview`
- `python -m hft_platform config validate`
- `make sync-symbols` (broker contract cache + rebuild)
- `python -m hft_platform wizard`

## 3. One-Command Start (Docker)
Build the image, start ClickHouse, and run the engine:
```bash
make start
```

## 4. Running Simulation (Local)
Simulation mode mocks the Feed and Execution, allowing you to test system stability and strategy logic without external connections.

```bash
# Start the platform in SIM mode
make run-sim
```
Use `Ctrl+C` to stop.
Check `logs/` for output.

## 5. Running Backtest
Backtest a strategy against historical (or mock) data.

```bash
# Run backtest with the strategy adapter (example)
python -m hft_platform backtest run \
  --data data/sample_feed.npz \
  --strategy-module hft_platform.strategies.simple_mm \
  --strategy-class SimpleMarketMaker \
  --strategy-id demo \
  --symbol 2330
```

## 6. Live Trading
**Prerequisite**: Shioaji Account (Sinopac).
Set credentials via Env Vars:
```bash
export SHIOAJI_PERSON_ID="YOUR_ID"
export SHIOAJI_PASSWORD="YOUR_PWD"
```

Run in LIVE mode:
```bash
python -m hft_platform run --mode live
```

## 7. Monitoring
*   **CLI Status**: `python -m hft_platform feed status`
*   **Metrics**: Prometheus endpoint at `http://localhost:9090` (if configured).
*   **Logs**: Structured JSON logs in `logs/app.jsonl`.
