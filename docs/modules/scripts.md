# scripts — Operational Scripts

> **Package**: `src/hft_platform/scripts/`
> **Files**: 7

## Overview

Standalone operational scripts for synthetic data generation, latency monitoring, futures subscription management, and options symbol rotation.

## Scripts

| Script | Purpose |
|--------|---------|
| `generate_synthetic_data.py` | Generate synthetic market data for testing |
| `latency_monitor.py` | Real-time latency monitoring |
| `subscribe_futures.py` | Futures contract subscription management |
| `refresh_options_symbols.py` | TXO expiry rotation |
| + 3 more | Additional operational tools |

## Usage

```bash
uv run python -m hft_platform.scripts.generate_synthetic_data
uv run python -m hft_platform.scripts.latency_monitor
uv run python -m hft_platform.scripts.refresh_options_symbols
```
