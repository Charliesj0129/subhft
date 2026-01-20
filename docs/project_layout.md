# Project Layout

This repo follows a `src/` layout and keeps runtime artifacts out of version control.
Use this map to find where to add new code and where to look when debugging.

## Top-Level Map

```
src/hft_platform/       Core Python package (services, strategy, risk, execution, recorder)
config/                 Config files (base defaults + env overrides)
.env.example            Environment variable template
docs/                   Documentation (start at docs/README.md)
tests/                  Unit/integration/system tests
examples/               Example strategies and scripts
notebooks/              Research notebooks and walkthroughs
scripts/                Local utilities and helpers
ops/                    Deployment and ops scripts
bin/                    Runtime scripts (startup, autostart)
rust_core/              Rust components (performance-critical pieces)
hftbacktest/            Backtest integration and docs
```

## Config Layout

- `config/base/` holds default templates tracked in git.
- `config/env/<mode>/` holds environment-specific overrides (sim/live).
- `config/settings.json` / `config/settings.py` are optional local overrides.
- `config/symbols.list` is the single source for `config/symbols.yaml`.
- `config/symbols.examples/` contains preset packs and demo lists.
- `config/contracts.json` caches broker contracts for rule expansion.

## Extension Points

- New strategy implementations: `src/hft_platform/strategies/`
- Strategy SDK changes: `src/hft_platform/strategy/`
- New alpha factors: `src/hft_platform/strategy/factors.py` or `src/hft_platform/features/`
- New services: `src/hft_platform/services/`
- New risk checks: `src/hft_platform/risk/`
- New recorder targets: `src/hft_platform/recorder/`
- New CLI commands: `src/hft_platform/cli.py`

## Generated / Local Artifacts (safe to ignore)

- Virtualenv and caches: `.venv/`, `.mypy_cache/`, `.hypothesis/`, `.pytest_cache/`, `.ruff_cache/`
- Local env file: `.env` (do not commit)
- Runtime output: `logs/`, `.wal/`, `data/`, `reports/`
- Build output: `target/`, `dist/`, `build/`
