# config

## Purpose
Configuration loading and environment/runtime overrides.

## Key Files
- `src/hft_platform/config/loader.py`: Settings load and merge chain.
- `src/hft_platform/config/wizard.py`: Interactive setup helper.
- `config/base/`: Versioned defaults.
- `config/env/<mode>/`: Environment overrides (sim/live).
- `config/env/<env>/`: Environment overlays (dev/staging/prod via `HFT_ENV`).
- `config/symbols.list`: Single source for symbols.
- `config/symbols.examples/`: Preset packs and demos.
- `.env.example`: Environment variable template.

## Settings Priority
1) `config/base/main.yaml`
2) `config/env/<mode>/main.yaml` (optional)
2.5) `config/env/<env>/main.yaml` (optional)
3) `config/settings.py` (optional)
4) Environment variables (`HFT_*`, `SHIOAJI_*`)
5) CLI overrides

## Common Config Files
- `config/symbols.yaml`: Symbol universe + price scale.
- `config/strategy_limits.yaml`: Risk limits per strategy.
- `config/order_adapter.yaml`: Rate limits and circuit breaker.
- `config/execution.yaml`: Execution settings.
- `config/recorder.yaml`: Recorder and ClickHouse settings.

## Usage
- Keep secrets in environment variables; use `.env.example` as a template and do not commit `.env`.
- Use `config/base/` as defaults and only override when needed.
- See `docs/config_reference.md` for the full reference.

## Symbols Shortcuts
- `make symbols` (build from `config/symbols.list`)
- `python -m hft_platform config preview`
- `python -m hft_platform config validate`
- `make sync-symbols` (refresh contract cache + rebuild)
- `python -m hft_platform wizard` (preset/manual/file import)
