# Security

- Never hardcode, print, commit, or chat secrets, API keys, broker credentials, account IDs, production tokens.
- Secrets live in `.env`, ignored config, or env vars. Broker prefixes stay isolated: `SHIOAJI_*`, `HFT_FUBON_*`, platform `HFT_*`.
- Never pass secrets as CLI args; scrub `structlog` fields.
- Broker APIs keep TLS verification enabled.
- Production ClickHouse must use auth; Prometheus/Grafana/Alertmanager ports must be firewalled.
- Do not commit `config/settings.py`, proprietary `symbols.yaml` annotations, data/WAL exports, or local reports with sensitive content.
- Production images run non-root and pin image versions.
