# Ops

Docker basics: `docker compose up -d --build`, `docker compose ps`, `docker compose logs -f hft-engine`. `make docker-clean` deletes volumes and ClickHouse data.

Ports: engine metrics 9090, ClickHouse 8123/9000, Redis 6379, Prometheus 9091, Grafana 3000, Alertmanager 9093.

Live-impacting config changes follow `docs/ops_change_control.md`: document what/why/risk/rollback, test in `HFT_MODE=sim`, watch metrics, keep rollback ready.

Important env: `HFT_MODE`, `HFT_ORDER_MODE`, `HFT_CLICKHOUSE_ENABLED`, `HFT_RECORDER_MODE`, `HFT_GATEWAY_ENABLED`, `HFT_OBS_POLICY`. Full reference via `hft-env-vars`.

Monitor WAL disk and ClickHouse health. Do not run production-impacting ops unless explicitly requested.
