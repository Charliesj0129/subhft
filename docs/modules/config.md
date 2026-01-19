# config

## Purpose
Configuration loading and runtime overrides.

## Key Files
- `src/hft_platform/config/loader.py`: Settings load and merge chain.
- `src/hft_platform/config/wizard.py`: Interactive setup helper.

## Settings Priority
1) `config/base/main.yaml`
2) `config/env/<mode>/main.yaml` (optional)
3) `config/settings.py` (optional)
4) Environment variables (`HFT_*`)
5) CLI overrides

## Common Config Files
- `config/symbols.yaml`: Symbol universe + price scale.
- `config/strategy_limits.yaml`: Risk limits per strategy.
- `config/order_adapter.yaml`: Rate limits and circuit breaker.
- `config/execution.yaml`: Execution settings.
- `config/recorder.yaml`: Recorder and ClickHouse settings.

## Environment Variables
See `docs/config_reference.md` for the full list.

## Extension Points
- Add new settings in YAML, then read via `loader.py`.
- Expose overrides via CLI or `.env`.
