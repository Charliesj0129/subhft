---
name: hft-env-vars
description: Complete reference table of HFT platform environment variables (HFT_*, SHIOAJI_*, CH_*) — runtime mode, broker selection, ClickHouse, monitor, reconnect, quote flap, storm guard, backup, startup reconciliation, Telegram. Use when configuring a new runtime/deployment, diagnosing why a feature isn't activating, auditing env-gated behavior, or adding a new env-driven toggle to the platform.
---

# HFT Platform Environment Variables (Complete Reference)

> For the everyday-essential short list (`HFT_MODE`, `HFT_ORDER_MODE`, `HFT_STRICT_PRICE_MODE`, `HFT_BROKER`), see CLAUDE.md. The full table below is for deep configuration.

| Variable                   | Default     | Purpose                                   |
| -------------------------- | ----------- | ----------------------------------------- |
| `HFT_MODE`                 | `sim`       | Runtime mode: `sim` / `real` / `replay`   |
| `HFT_ORDER_MODE`           | `sim`       | Order execution: `sim` / `live` (LIVE = real money) |
| `HFT_SYMBOLS`              | —           | Comma-separated symbol list override      |
| `HFT_QUOTE_VERSION`        | `auto`      | Shioaji quote protocol version            |
| `HFT_STRICT_PRICE_MODE`    | `0`         | `1` = reject float prices with TypeError  |
| `HFT_GATEWAY_ENABLED`      | `0`         | `1` = enable CE-M2 order/risk gateway     |
| `HFT_RECORDER_MODE`        | `direct`    | `wal_first` = WAL-only write path (CE-M3) |
| `HFT_CLICKHOUSE_HOST`      | `localhost` | ClickHouse host                           |
| `HFT_EXPOSURE_MAX_SYMBOLS` | `10000`     | ExposureStore cardinality bound           |
| `HFT_BROKER`               | `shioaji`   | Broker backend: `shioaji` / `fubon`       |
| `HFT_FEATURE_ENGINE_ENABLED` | `1`         | `0` = disable FeatureEngine in runtime pipeline (default: v3 with 27 features) |
| `HFT_FUSED_NORMALIZER`     | `0`         | `1` = enable fused Rust normalizer+LOB pipeline |
| `HFT_FEATURE_ENGINE_BACKEND` | `python`  | Backend for FeatureEngine: `python` / `rust`    |
| `HFT_FUBON_CERT_PATH`      | —           | Fubon API certificate file path           |
| `HFT_FUBON_ACCOUNT`        | —           | Fubon trading account ID                  |
| `HFT_FUBON_PASSWORD`       | —           | Fubon account password (use secret mgr)   |
| `HFT_MONITOR_SOURCE`       | `clickhouse`| Monitor data source: `clickhouse`/`redis`/`hybrid` |
| `HFT_MONITOR_LIVE_ENABLED` | `0`         | `1` = enable Redis live publisher in MarketDataService |
| `HFT_MONITOR_REDIS_HOST`   | `localhost` | Redis host for monitor live cache         |
| `HFT_MONITOR_REDIS_PORT`   | `6379`      | Redis port for monitor live cache         |
| `HFT_MONITOR_REDIS_PASSWORD`| —          | Redis password for monitor live cache     |
| `HFT_MONITOR_DATA_SOURCE`  | `auto`      | Data source layer: `ch`/`shm`/`auto`     |
| `HFT_RECONNECT_HOURS`     | `08:30-13:35`| Trading hours window for auto-reconnect  |
| `HFT_RECONNECT_HOURS_2`   | —           | Secondary trading hours window            |
| `HFT_RECONNECT_COOLDOWN`  | `60`        | Reconnect cooldown seconds                |
| `HFT_RECONNECT_BACKOFF_S` | `5`         | Initial reconnect backoff delay seconds   |
| `HFT_RECONNECT_BACKOFF_MAX_S`| `120`    | Maximum reconnect backoff delay seconds   |
| `HFT_QUOTE_FLAP_THRESHOLD`| `5`         | Quote flap detection: max flaps in window |
| `HFT_QUOTE_FLAP_WINDOW_S` | `60`        | Quote flap detection window seconds       |
| `HFT_QUOTE_FLAP_COOLDOWN_S`| `300`      | Quote flap cooldown before re-subscribe   |
| `HFT_STORMGUARD_FEED_GAP_STORM_S`| `1.0` | Feed gap threshold (seconds) to trigger STORM. Feed gap alone cannot trigger HALT. |
| `HFT_STORMGUARD_FEED_GAP_HALT_S`| `30`  | **Deprecated** alias for `_STORM_S`. Maps to STORM (not HALT). |
| `HFT_BACKUP_ENABLED`        | `0`                    | `1` = enable automated daily ClickHouse backup |
| `HFT_BACKUP_RETAIN_DAYS`    | `30`                   | Number of daily backups to retain               |
| `CH_BACKUP_PATH`            | `./backups/clickhouse`  | Host path for ClickHouse backup volume mount    |
| `HFT_STARTUP_RECON_ENABLED`              | `1`   | Enable startup position recovery            |
| `HFT_STARTUP_RECON_QTY_THRESHOLD`        | `10`  | Stock discrepancy auto-correct threshold    |
| `HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD`| `2`   | Futures discrepancy auto-correct threshold  |
| `HFT_CHECKPOINT_ENABLED`                 | `1`   | Enable periodic position checkpoint writing |
| `HFT_ORDER_SHADOW_MODE`  | `0`         | `1` = shadow order interception (orders never reach broker) |
| `HFT_RECONNECT_DAYS`    | `mon,tue,wed,thu,fri` | Weekdays for auto-reconnect              |
| `HFT_RECONNECT_TZ`      | `Asia/Taipei`| Timezone for reconnect hours             |
| `HFT_ARCHIVE_RETENTION_DAYS` | `3`    | WAL archive retention days                |
| `HFT_TELEGRAM_ENABLED`  | `0`         | `1` = enable Telegram notification bot    |
| `HFT_TELEGRAM_BOT_TOKEN`| —           | Telegram bot token (use secret mgr)       |
| `HFT_TELEGRAM_CHAT_ID`  | —           | Telegram chat ID for alerts               |
