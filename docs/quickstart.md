# HFT Platform Quickstart

## 1. Installation
The project uses `uv` for dependency management, or standard `pip`.

```bash
# Clone
git clone <repo>
cd hft_platform

# Install dependencies
uv sync
# OR
pip install -r requirements.txt
```

## 2. Configuration
Core configuration lives in `config/`.
*   `symbols.yaml`: Define which instruments to trade/subscribe.
*   `strategies.yaml`: Define active strategies and parameters.
*   `strategy_limits.yaml`: Risk limits (Max Position, Max Order Size).

**Example `symbols.yaml`**:
```yaml
symbols:
  - code: "2330"
    exchange: "TSE"
```

## 3. Running Simulation
Simulation mode mocks the Feed and Execution, allowing you to test system stability and strategy logic without external connections.

```bash
# Start the platform in SIM mode
python -m hft_platform run --mode sim
```
Use `Ctrl+C` to stop.
Check `logs/` for output.

## 4. Running Backtest
Backtest a strategy against historical (or mock) data.

```bash
# Run backtest for 'AdvancedMarketMaker' (example)
# Note: Ensure the strategy class is available in python path
python -m hft_platform backtest run --strategy advanced_mm --symbol 2330 --date 2024-01-01
```

## 5. Live Trading
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

## 6. Monitoring
*   **CLI Status**: `python -m hft_platform feed status`
*   **Metrics**: Prometheus endpoint at `http://localhost:9090` (if configured).
*   **Logs**: Structured JSON logs in `logs/app.jsonl`.
