# Config Directory Structure

## Layout

```
config/
  base/                 # Defaults & templates — loaded by config/loader.py as base layer
    main.yaml           # Base settings (mode, strategy, paths, prometheus_port)
    symbols.yaml        # Default symbol definitions (template)
    strategies.yaml     # Strategy registry (full catalog)
    strategy_limits.yaml
    order_adapter.yaml
    brokers/            # Broker-specific defaults (shioaji.yaml, fubon.yaml)
    fees/               # Fee schedule definitions
  env/                  # Environment-specific overrides (loaded after base)
    sim/main.yaml
    live/main.yaml
  monitoring/           # Prometheus, Loki, Alertmanager configs
  research/             # Research-specific configs (latency profiles)
  clickhouse_*.xml      # ClickHouse server-side configs (mounted into Docker)
  symbols.yaml          # RUNTIME override — written by wizard/tools, used by live system
  strategies.yaml       # RUNTIME override — written by wizard/tools
  order_adapter.yaml    # Legacy (unused) — base/order_adapter.yaml is canonical
  risk.yaml             # Runtime risk config
  watchlist.yaml        # Symbol watchlist for monitoring
  watchlist_live.yaml   # Live trading watchlist
  settings.py           # Per-machine Python overrides (gitignored)
```

## Priority Chain (config/loader.py)

```
base/main.yaml → env/{mode}/main.yaml → settings.py → Environment Variables → CLI Overrides
```

## Key Distinction

- `config/base/*.yaml` = **templates/defaults** (committed, rarely changed)
- `config/*.yaml` (root level) = **runtime overrides** (written by tools, may diverge from base)
- `config/settings.py` = **per-machine secrets/overrides** (gitignored)
