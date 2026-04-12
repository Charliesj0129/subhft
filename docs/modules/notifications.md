# notifications — Alert Routing

> **Package**: `src/hft_platform/notifications/`
> **Runtime Plane**: Observability

## Overview

Event-to-notification routing with Telegram (primary), webhook (fallback), and AlertManager bridge channels. 23+ message templates with HTML sanitization.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `dispatcher.py` | `NotificationDispatcher` | Routes events to channels (18 notification methods) |
| `telegram.py` | `TelegramSender`, `TelegramCommandPoller` | Telegram Bot API with retry + command polling |
| `webhook.py` | `WebhookSender` | Generic webhook (Discord, LINE, Slack) |
| `alertmanager_bridge.py` | `AlertmanagerBridge` | Alertmanager → Telegram HTTP bridge |
| `templates.py` | `render_halt()`, `render_daily_report()`, ... | 23+ HTML-safe message templates |

## Critical vs Non-Critical

| Type | Channels | Rate Limited | Retry |
|------|----------|-------------|-------|
| Critical | Telegram + Webhook | No | 3x exponential backoff |
| Non-critical | Telegram only | Yes (1/s) | None |

### Critical Events
- `notify_halt(reason)` — Trading halted
- `notify_daily_loss(pnl, limit)` — Daily loss limit breached
- `notify_margin_critical(ratio, used, available)` — Margin critical
- `notify_position_recovery_failed(...)` — Startup recovery failed
- `notify_autonomy_transition(...)` — when to_mode=="HALT"

### Non-Critical Events
- `notify_stormguard_change(...)`, `notify_reconnect(...)`, `notify_daily_report(...)`, `notify_heartbeat(...)`, etc.

## TelegramCommandPoller

Polls Telegram `getUpdates` for interactive commands:
- `/stop` → Sets `hft:emergency_halt` in Redis
- `/status` → Replies "Status: running"

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_TELEGRAM_BOT_TOKEN` | — | Telegram Bot API token |
| `HFT_TELEGRAM_CHAT_ID` | — | Target chat ID |
| `HFT_WEBHOOK_URL` | — | Fallback webhook URL |
| `HFT_ALERT_BRIDGE_PORT` | `8081` | AlertManager bridge port |
