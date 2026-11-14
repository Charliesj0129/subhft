# Alert Routing Setup Guide (WU-11)

Production AlertManager configuration for the HFT Platform.

## Prerequisites

- AlertManager v0.27+ deployed (via Docker Compose or standalone)
- Prometheus configured with `alertmanager_config` pointing to AlertManager
- Network access from AlertManager to Telegram API and Slack webhook endpoints
- Alert rules loaded in Prometheus (see `config/monitoring/alerts/`)

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ALERTMANAGER_TELEGRAM_BOT_TOKEN` | Yes (critical alerts) | Telegram Bot API token from @BotFather |
| `ALERTMANAGER_TELEGRAM_CHAT_ID` | Yes (critical alerts) | Target chat/group ID for critical notifications |
| `ALERTMANAGER_SLACK_WEBHOOK_URL` | Yes (warning alerts) | Slack incoming webhook URL |

Store these in `.env` or inject via your secret manager. Never commit actual values.

## Deployment

1. Copy the example config:

   ```bash
   cp config/monitoring/alerts/alertmanager.prod.example.yml \
      config/monitoring/alerts/alertmanager.prod.yml
   ```

2. Replace `$ALERTMANAGER_*` placeholders with actual values (or use envsubst):

   ```bash
   envsubst < config/monitoring/alerts/alertmanager.prod.example.yml \
            > config/monitoring/alerts/alertmanager.prod.yml
   ```

3. Validate the config:

   ```bash
   amtool check-config config/monitoring/alerts/alertmanager.prod.yml
   ```

4. Deploy (Docker Compose):

   ```bash
   docker compose up -d alertmanager
   ```

5. Verify AlertManager is running:

   ```bash
   curl -s http://localhost:9093/-/healthy
   ```

## Telegram bridge location (webhook receiver)

The Alertmanager -> Telegram bridge (`AlertmanagerBridge`, `POST /webhook/alertmanager`,
port 8081) runs inside the **`hft-bot`** container (`python -m hft_platform.bot`), not
the trading engine. The engine can be stopped or down while alerts (above all
`HFTEngineDown`) still reach Telegram. On 2026-11-11 the bridge lived in `hft-engine`
and Alertmanager gave up 18 notifications with "lookup hft-engine" / "connection
refused".

- Alertmanager webhook url: `http://hft-bot:8081/webhook/alertmanager`
  (not `hft-engine:8081`).
- `hft-bot` needs `HFT_TELEGRAM_BOT_TOKEN` / `HFT_TELEGRAM_CHAT_ID` in its environment
  (the bridge sends with the same `TelegramSender` env names).
- Kill-switch: `HFT_ALERTMANAGER_BRIDGE_ENABLED=0` in the `hft-bot` environment leaves
  the bridge off (default `1`).

**Deploy order.** Deploy `hft-bot` with the bridge and switch Alertmanager's webhook url
to `hft-bot:8081` (reload Alertmanager) BEFORE any engine deploy that drops the engine's
bridge. Done the other way round, alerts are undeliverable in between: the old url
points at an engine that no longer serves the bridge.

**Network precondition.** `alertmanager` must be able to resolve `hft-bot`. On THESHOW
(checked 2026-11-13, `docker inspect`) `hft-bot`, `alertmanager` and `hft-engine` all sit
on `subhft_default`. The repo's `docker-compose.production.yml` and
`docker-compose.prod.locked.yml` put `alertmanager` on `hft-internal` and leave `hft-bot`
on `default`; a stack started from those files cannot reach the bridge, so every alert
fails. Before switching the url, confirm from the host that the two containers share a
network:

```bash
docker inspect hft-bot alertmanager --format '{{.Name}} {{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
```

If they do not share one, add `hft-bot` to `hft-internal` first (a protected compose
edit) and do not switch the url until it is deployed.

## Testing

### Send a test alert

```bash
# Critical test
curl -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{
    "labels": {
      "alertname": "TestCritical",
      "severity": "critical",
      "symbol": "2330"
    },
    "annotations": {
      "summary": "Test critical alert",
      "description": "This is a test. Safe to ignore."
    }
  }]'

# Warning test
curl -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{
    "labels": {
      "alertname": "TestWarning",
      "severity": "warning",
      "symbol": "2330"
    },
    "annotations": {
      "summary": "Test warning alert",
      "description": "This is a test. Safe to ignore."
    }
  }]'
```

### Verify routing

```bash
amtool config routes test --config.file=config/monitoring/alerts/alertmanager.prod.yml \
  severity=critical alertname=StormGuardHalt
# Expected: telegram-critical

amtool config routes test --config.file=config/monitoring/alerts/alertmanager.prod.yml \
  severity=warning alertname=ReconciliationDrift
# Expected: slack-warning

amtool config routes test --config.file=config/monitoring/alerts/alertmanager.prod.yml \
  severity=info alertname=HeartbeatOk
# Expected: null
```

## Severity Mapping

| Severity | Receiver | Examples |
|---|---|---|
| `critical` | Telegram (immediate) | StormGuard HALT, position mismatch > threshold, feed gap > 30s, kill switch activated |
| `warning` | Slack | Reconciliation drift, queue depth high, ClickHouse write latency, WAL backlog growing |
| `info` | Null (suppressed) | Heartbeat, config reload, routine maintenance |

## Customization

### Adding a new receiver

Add a new entry under `receivers:` in the YAML and a corresponding route under `route.routes`.

### Changing group timing

- `group_wait`: How long to buffer before sending the first notification for a new group.
- `group_interval`: Minimum interval between notifications for the same group.
- `repeat_interval`: How long before re-sending an unresolved alert.

For critical alerts, shorter intervals are configured by default (10s wait, 1m interval, 15m repeat).

### Inhibition rules

The default config inhibits `warning` alerts when a `critical` alert fires for the same `alertname`. Add additional inhibit rules if needed (e.g., suppress symbol-level warnings during a platform-wide HALT).
