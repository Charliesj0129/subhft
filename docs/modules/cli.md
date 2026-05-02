# cli — Command-Line Interface

> **Package**: `src/hft_platform/cli/`
> **Runtime Plane**: Control (entry point)

## Overview

CLI entry point (`hft` command) with 13 subcommand modules covering runtime, alpha governance, feature management, health checks, and operational tools.

## Commands

| Command | Module | Purpose |
|---------|--------|---------|
| `hft run sim\|live` | `_run.py` | Start trading runtime |
| `hft init` | — | Initialize project structure |
| `hft check` | `_checks.py` | Pre-flight validation |
| `hft wizard` | — | Interactive setup wizard |
| `hft alpha` | `_alpha.py` | Alpha governance pipeline |
| `hft feature` | `_feature.py` | Feature engine management |
| `hft config` | — | Config inspection |
| `hft backtest` | — | Backtesting runner |
| `hft recorder` | — | Recorder operations |
| `hft diag` | — | Diagnostics tools |
| `hft feed` | — | Feed adapter tools |
| `hft health` | `_health.py` | Health check queries |
| `hft ops` | `_ops.py` | Operational tools |
| `hft risk` | `_risk.py` | Risk management tools |
| `hft symbols` | `_symbols.py` | Symbol resolution |
| `hft tca` | `_tca.py` | Transaction cost analysis |
| `hft feasibility` | `_feasibility.py` | Alpha feasibility checks |
| `hft golive` | `_golive.py` | Go-live readiness checks |

## Usage

```bash
uv run hft run sim                    # Start in simulation mode
uv run hft run live                   # Start in live mode (real money!)
uv run hft check                     # Pre-flight validation
uv run hft alpha validate <alpha_id> # Run alpha validation gates
uv run hft feature status            # Feature engine status
```

## Entry Point

Defined in `pyproject.toml`:
```toml
[project.scripts]
hft = "hft_platform.cli:main"
```
