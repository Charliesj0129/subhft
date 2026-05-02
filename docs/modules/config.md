# config — Layered Configuration System

> **Package**: `src/hft_platform/config/`
> **Runtime Plane**: Control
> **Files**: 10

## Overview

5-layer configuration merge with msgspec validation, symbol DSL, hot-reload for strategy limits, and setup wizard.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `loader.py` | `load_config()`, `merge_config()` | 5-layer config merge engine |
| `schema.py` | `HFTConfig`, `StrategyLimitsConfig` | msgspec-validated config schema |
| `symbols.py` | `SymbolDSL`, `resolve_symbols()` | Symbol list DSL with tags and filters |
| `hot_reload.py` | `HotReloadWatcher` | SIGHUP-triggered strategy limits reload |
| `wizard.py` | `ConfigWizard` | Interactive setup wizard |
| + 5 more | — | Supporting utilities |

## 5-Layer Priority Chain

```
1. Base YAML    → config/base/main.yaml
2. Env YAML     → config/env/{mode}/main.yaml
3. settings.py  → config/settings.py (per-machine, .gitignored)
4. Environment  → HFT_MODE, HFT_SYMBOLS, ...
5. CLI Override  → --mode, --symbols, ...
```

Higher layers override lower. Environment variables always win over YAML.

## Config Schema (msgspec)

```python
@struct
class HFTConfig:
    mode: str                    # sim | real | replay
    broker: str                  # shioaji | fubon
    symbols: list[str]
    strategies: list[dict]
    risk: RiskConfig
    recorder: RecorderConfig
    ...
```

Validated at load time — invalid config raises immediately.

## Symbol DSL

```yaml
# Static list
symbols: [2330, 2317, TXFD6]

# Tag-based
symbols:
  - tag:tech
  - tag:large_cap

# Product type filter
symbols:
  - product_type:FUTURES
```

Resolved via `SymbolMetadata` from broker contracts.

## Hot-Reload

```bash
kill -SIGHUP $(pgrep -f "hft run")  # Reload strategy limits
```

- Watches `config/base/strategy_limits.yaml`
- Updates risk validator parameters without restart
- Logs reload events

## Key Config Files

| File | Purpose |
|------|---------|
| `config/base/main.yaml` | Base configuration |
| `config/base/strategies.yaml` | Strategy definitions |
| `config/base/strategy_limits.yaml` | Risk limits (hot-reloadable) |
| `config/base/session_governor.yaml` | Session phase schedule |
| `config/base/brokers/shioaji.yaml` | Shioaji-specific config |
| `config/base/brokers/fubon.yaml` | Fubon-specific config |
| `config/env/sim/main.yaml` | Simulation overrides |
| `config/env/real/main.yaml` | Production overrides |
