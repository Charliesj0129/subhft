---
name: config-env
description: Configure HFT platform environment variables, YAML config files, and runtime settings. Covers the full config priority chain, broker credentials, and feature flags.
---

# Config & Environment

## When to Use

- Setting up a new environment (sim/live/replay)
- Changing broker, enabling features, or tuning parameters
- Debugging config resolution issues
- Reviewing which env vars override which config

## Config Priority Chain

Settings resolve via layered merge (later overrides earlier):

```
Base YAML (config/base/main.yaml)
  -> Env YAML (config/env/{mode}/main.yaml)
    -> settings.py (config/settings.py)
      -> Environment Variables (HFT_*)
        -> CLI Overrides (--mode, --symbols, ...)
```

Loader implementation: `src/hft_platform/config/loader.py`

## Environment Variables

### Runtime

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_MODE` | `sim` | Runtime mode: `sim` / `live` / `replay` |
| `HFT_SYMBOLS` | -- | Comma-separated symbol list override |
| `HFT_QUOTE_VERSION` | `auto` | Shioaji quote protocol version |

### Broker Selection

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_BROKER` | `shioaji` | Broker backend: `shioaji` / `fubon` |
| `SHIOAJI_API_KEY` | -- | Shioaji API key |
| `SHIOAJI_SECRET_KEY` | -- | Shioaji secret key |
| `HFT_FUBON_CERT_PATH` | -- | Fubon API certificate file path |
| `HFT_FUBON_ACCOUNT` | -- | Fubon trading account ID |
| `HFT_FUBON_PASSWORD` | -- | Fubon account password (use secret manager) |

### Feature Flags

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_FEATURE_ENGINE_ENABLED` | `1` | `0` = disable FeatureEngine in pipeline |
| `HFT_FEATURE_ENGINE_BACKEND` | `python` | Backend: `python` / `rust` |
| `HFT_FUSED_NORMALIZER` | `0` | `1` = enable fused Rust normalizer+LOB pipeline |
| `HFT_GATEWAY_ENABLED` | `0` | `1` = enable CE-M2 order/risk gateway |
| `HFT_STRICT_PRICE_MODE` | `0` | `1` = reject float prices with TypeError |

### Infrastructure

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_CLICKHOUSE_HOST` | `localhost` | ClickHouse host |
| `HFT_RECORDER_MODE` | `direct` | `wal_first` = WAL-only write path |

### Monitoring

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_MONITOR_SOURCE` | `clickhouse` | Monitor data source: `clickhouse`/`redis`/`hybrid` |
| `HFT_MONITOR_LIVE_ENABLED` | `0` | `1` = enable Redis live publisher |
| `HFT_MONITOR_REDIS_HOST` | `localhost` | Redis host for monitor cache |
| `HFT_MONITOR_REDIS_PORT` | `6379` | Redis port for monitor cache |
| `HFT_MONITOR_REDIS_PASSWORD` | -- | Redis password for monitor cache |
| `HFT_MONITOR_DATA_SOURCE` | `auto` | Data source layer: `ch`/`shm`/`auto` |

### Reconnect & Resilience

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_RECONNECT_HOURS` | `08:30-13:35` | Trading hours window for auto-reconnect |
| `HFT_RECONNECT_HOURS_2` | -- | Secondary trading hours window |
| `HFT_RECONNECT_COOLDOWN` | `60` | Reconnect cooldown seconds |
| `HFT_RECONNECT_BACKOFF_S` | `5` | Initial reconnect backoff delay seconds |
| `HFT_RECONNECT_BACKOFF_MAX_S` | `120` | Maximum reconnect backoff delay seconds |

### Safety & Limits

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_EXPOSURE_MAX_SYMBOLS` | `10000` | ExposureStore cardinality bound |
| `HFT_STORMGUARD_FEED_GAP_HALT_S` | `30` | Feed gap threshold to trigger HALT |

### Quote Flap Detection

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_QUOTE_FLAP_THRESHOLD` | `5` | Max flaps in detection window |
| `HFT_QUOTE_FLAP_WINDOW_S` | `60` | Flap detection window seconds |
| `HFT_QUOTE_FLAP_COOLDOWN_S` | `300` | Cooldown before re-subscribe after flap |

## Config Files

| File | Purpose |
|------|---------|
| `config/base/main.yaml` | Base configuration (all modes) |
| `config/env/sim/main.yaml` | Sim mode overrides |
| `config/env/live/main.yaml` | Live mode overrides |
| `config/env/replay/main.yaml` | Replay mode overrides |
| `config/base/brokers/shioaji.yaml` | Shioaji-specific config |
| `config/base/brokers/fubon.yaml` | Fubon-specific config |
| `config/settings.py` | Per-machine overrides (gitignored) |
| `config/research/latency_profiles.yaml` | Broker latency profiles for backtesting |

## .env File Template

Create a `.env` file in the project root (gitignored):

```bash
# Runtime
HFT_MODE=sim
HFT_SYMBOLS=2330,2317,2454

# Broker (choose one set)
HFT_BROKER=shioaji
SHIOAJI_API_KEY=your_api_key_here
SHIOAJI_SECRET_KEY=your_secret_key_here

# Or for Fubon:
# HFT_BROKER=fubon
# HFT_FUBON_CERT_PATH=/path/to/cert.pfx
# HFT_FUBON_ACCOUNT=your_account
# HFT_FUBON_PASSWORD=your_password

# Infrastructure
HFT_CLICKHOUSE_HOST=localhost
HFT_RECORDER_MODE=direct

# Features
HFT_FEATURE_ENGINE_ENABLED=1
HFT_FEATURE_ENGINE_BACKEND=python
HFT_FUSED_NORMALIZER=0
HFT_GATEWAY_ENABLED=0
```

## Security Rules

- NEVER hardcode API keys, passwords, or tokens in source code
- Store secrets in `.env` (local) or environment variables (Docker/production)
- `.env` is in `.gitignore` -- verify with `git check-ignore .env`
- Each broker uses distinct env var prefixes (Shioaji: `SHIOAJI_*`, Fubon: `HFT_FUBON_*`)
- Rotate any secrets that may have been exposed
- Never pass secrets as CLI arguments (visible in `ps aux`)
- `config/settings.py` is gitignored by convention -- never commit it
