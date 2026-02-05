# cli

## Purpose
Command-line entry point for running the system, generating configs, and quick diagnostics.

## Key Files
- `src/hft_platform/cli.py`: Argument parsing and command handlers.
- `src/hft_platform/__main__.py`: Entry point for `python -m hft_platform`.

CLI entrypoint (after install): `hft`

## Commands
- `run sim|live|replay`: Run the pipeline.
- `init`: Generate `config/settings.py` and strategy skeleton.
- `check`: Validate config; optional export.
- `wizard`: Interactive configuration wizard.
- `feed status`: Check Prometheus metrics.
- `diag`: Quick diagnostics.
- `strat test`: Smoke test a strategy without live data.
- `backtest convert|run`: Backtest utilities.
- `config resolve`: Resolve exchange codes for symbols.

## Examples
```bash
python -m hft_platform run sim
python -m hft_platform init --strategy-id my_strategy --symbol 2330
python -m hft_platform check --export json
python -m hft_platform feed status --port 9090
```

## Notes
- CLI uses `config/loader.py` to merge settings.
- `run live` will downgrade to `sim` if credentials are missing.
