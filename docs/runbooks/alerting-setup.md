# AlertManager Setup Guide

## Overview

AlertManager routes Prometheus alerts to Slack (warnings) and PagerDuty (critical).
Configuration lives in `config/monitoring/alerts/`.

---

## Required Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_WEBHOOK_URL` | Yes | Slack incoming webhook URL for the #hft-alerts channel |
| `PAGERDUTY_INTEGRATION_KEY` | Yes (prod) | PagerDuty Events API v2 integration key |
| `ALERTMANAGER_EXTERNAL_URL` | No | Public URL for AlertManager links in notifications |

Set these in `.env` or export them before starting services:

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/T00/B00/xxxx"
export PAGERDUTY_INTEGRATION_KEY="abcdef1234567890abcdef1234567890"
```

---

## Severity Routing

| Severity | Route | Response Time | Examples |
|----------|-------|---------------|---------|
| `critical` | PagerDuty + Slack | Immediate (page on-call) | HALT triggered, CH down, feed dead |
| `warning` | Slack only | 15 minutes | Queue depth high, WAL growing, latency elevated |
| `info` | Slack (low-priority channel) | Best effort | Daily summary, config reload |

Routing is configured in `config/monitoring/alerts/alertmanager.production.yml`.

---

## Configuration Files

### Development (default)

```
config/monitoring/alerts/alertmanager.yml  # Logs to stdout, no external routing
```

### Production

```
config/monitoring/alerts/alertmanager.production.yml  # PagerDuty + Slack
```

To use production config, set in `docker-compose.yml` or override:

```yaml
alertmanager:
  volumes:
    - ./config/monitoring/alerts/alertmanager.production.yml:/etc/alertmanager/alertmanager.yml
```

---

## Alert Grouping

Alerts are grouped by `alertname` and `strategy` to reduce noise:
- Multiple symbols triggering the same alert on the same strategy are batched.
- Group wait: 30s (initial), Group interval: 5m (subsequent).
- Repeat interval: 4h for warnings, 1h for critical.

---

## Testing Alerts

### 1. Fire a Test Alert

```bash
# Send a test alert to AlertManager
curl -X POST http://localhost:9093/api/v2/alerts \
  -H "Content-Type: application/json" \
  -d '[{
    "labels": {
      "alertname": "TestAlert",
      "severity": "warning",
      "strategy": "test"
    },
    "annotations": {
      "summary": "Test alert from ops team",
      "description": "This is a test alert. Please ignore."
    },
    "startsAt": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"
  }]'
```

### 2. Verify Routing

```bash
# Check AlertManager status
curl -s http://localhost:9093/api/v2/status | python3 -m json.tool

# Check active alerts
curl -s http://localhost:9093/api/v2/alerts | python3 -m json.tool
```

### 3. Verify Slack Delivery

- Check #hft-alerts channel for the test alert.
- If not received, verify `SLACK_WEBHOOK_URL` and test directly:
  ```bash
  curl -X POST "$SLACK_WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d '{"text": "AlertManager connectivity test"}'
  ```

### 4. Verify PagerDuty Delivery

- Fire a test critical alert (same curl as above, change severity to `critical`).
- Check PagerDuty service for the incident.
- Resolve the test incident promptly.

---

## Silencing Alerts

During planned maintenance, silence alerts to avoid false pages:

```bash
# Silence all alerts for 2 hours
curl -X POST http://localhost:9093/api/v2/silences \
  -H "Content-Type: application/json" \
  -d '{
    "matchers": [{"name": "alertname", "value": ".*", "isRegex": true}],
    "startsAt": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'",
    "endsAt": "'$(date -u -d "+2 hours" +%Y-%m-%dT%H:%M:%SZ)'",
    "createdBy": "ops",
    "comment": "Planned maintenance window"
  }'
```

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| No Slack messages | Verify `SLACK_WEBHOOK_URL`, check AlertManager logs |
| No PagerDuty pages | Verify `PAGERDUTY_INTEGRATION_KEY`, check service is active |
| Duplicate alerts | Check group_by labels, increase group_interval |
| Too many alerts | Add inhibition rules in alertmanager config |
| Alert not firing | Check Prometheus rules: `curl http://localhost:9091/api/v1/rules` |
