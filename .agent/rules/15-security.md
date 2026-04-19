# Security Rules

## Credentials

- NEVER hardcode API keys, passwords, or tokens.
- Store secrets in `.env` (local) or env vars (Docker/prod). Verify `git check-ignore .env` returns `.env`.
- Env var prefixes: `HFT_*` (platform), `SHIOAJI_*` (Shioaji), `HFT_FUBON_*` (Fubon). Never share across brokers.

## Logging

- NEVER log credential values; `structlog` processors MUST scrub sensitive fields.
- Prefixes OK (`api_key_prefix=ABC***`); full values forbidden.
- Never pass secrets as CLI args (visible in `ps aux`).

## Network

- Broker API (`ShioajiClient`, Fubon) uses TLS. Never disable cert verification.
- ClickHouse default has no auth; in prod set `HFT_CLICKHOUSE_USER` + `HFT_CLICKHOUSE_PASSWORD`.
- Prometheus/Grafana ports (9090, 9091, 3000) MUST be firewalled in prod.

## Config & Docker

- `config/settings.py` is `.gitignore`-d (may contain per-machine overrides).
- Never commit `symbols.yaml` with proprietary annotations.
- Production images use non-root user (`--user hft` in Dockerfile).
- Pin image versions (e.g., `clickhouse-server:25.12.3`, not `:latest`).
