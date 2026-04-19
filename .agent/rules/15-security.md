# Security Rules

## Credentials

- **NEVER** hardcode API keys, passwords, or tokens in source code.
- Store secrets in `.env` (local) or environment variables (Docker/production).
- `.env` is in `.gitignore`. Verify: `git check-ignore .env` should return `.env`.
- Shioaji keys use `SHIOAJI_API_KEY` and `SHIOAJI_SECRET_KEY` env vars.
- Prefixed env vars: `HFT_*` for platform config, `SHIOAJI_*` for broker.

## Logging

- **NEVER** log credential values. `structlog` processors should scrub sensitive fields.
- Log keys/identifiers are OK (e.g., `api_key_prefix=ABC***`), but NEVER full values.
- Do not pass secrets as CLI arguments (visible in `ps aux`). Use env vars.

## Network

- Broker API connections (`ShioajiClient`) use TLS. Never disable certificate verification.
- ClickHouse default setup has no auth. In production, set `HFT_CLICKHOUSE_USER` + `HFT_CLICKHOUSE_PASSWORD`.
- Prometheus/Grafana ports (9090, 9091, 3000) should be firewalled in production.

## Configuration Files

- `config/settings.py` may override secrets. It is `.gitignore`-d by convention.
- Never commit `symbols.yaml` if it contains proprietary symbol lists with internal annotations.

## Docker

- Production images should use non-root user (already set: `--user hft` in Dockerfile).
- Pin image versions for reproducibility (e.g., `clickhouse-server:25.12.3`, not `:latest`).
