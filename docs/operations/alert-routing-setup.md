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
