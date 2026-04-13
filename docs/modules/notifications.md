# notifications — Alert Routing

> **Package**: `src/hft_platform/notifications/`
> **Files**: 6 (`dispatcher.py`, `telegram.py`, `webhook.py`, `alertmanager_bridge.py`, `templates.py`, `__init__.py`)
> **Runtime Plane**: Observability (wired into trading pipeline via callbacks)

## Overview

Event-to-notification routing with Telegram (primary), webhook (fallback), and Alertmanager bridge channels. 23+ HTML-safe message templates. Critical events bypass rate limiting and retry with exponential backoff; non-critical events are rate-limited to 1 msg/sec.

Wired into the trading pipeline at bootstrap — receives callbacks from RiskEngine, StormGuard, AutonomyMonitor, ReconciliationService, and DailyReportService.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `dispatcher.py` | `NotificationDispatcher` | Routes platform events to channels. 18+ `notify_*` methods, each maps to one template. Sets `critical` flag per event type |
| `telegram.py` | `TelegramSender`, `TelegramCommandPoller` | Telegram Bot API client: rate limiting, critical retry (3x exponential backoff), message chunking (>4096 chars), session management |
| `webhook.py` | `WebhookSender` | Generic webhook sender for fallback (Discord, LINE, Slack) |
| `alertmanager_bridge.py` | `AlertmanagerBridge` | Raw asyncio HTTP server — receives Alertmanager webhook POSTs, forwards to Telegram. Port 8081 (configurable) |
| `templates.py` | `render_halt()`, `render_daily_report()`, ... | 23+ HTML-safe message rendering functions. All use `html.escape()` |

## Critical vs Non-Critical Events

| Type | Channels | Rate Limited | Retry | Example Events |
|------|----------|-------------|-------|----------------|
| Critical | Telegram + Webhook (parallel) | No | 3x exponential backoff (1s, 2s, 4s) | HALT, daily loss limit, margin critical, position recovery failed, autonomy→HALT |
| Non-critical | Telegram only | Yes (1/sec) | None | StormGuard change, reconnect, daily report, heartbeat, weekly summary, session phase |

### Critical Events (18+ methods)

| Method | Template | Trigger |
|--------|----------|---------|
| `notify_halt(reason)` | `render_halt` | Trading halted (any cause) |
| `notify_daily_loss(pnl, limit)` | `render_daily_loss` | Daily PnL < configured limit |
| `notify_margin_critical(ratio, used, available)` | `render_margin_critical` | Margin usage above threshold |
| `notify_position_recovery_failed(...)` | `render_position_recovery_failed` | Startup reconciliation failure |
| `notify_autonomy_transition(...)` | `render_autonomy_transition` | AutonomyMonitor → HALT |

### Non-Critical Events

| Method | Template | Trigger |
|--------|----------|---------|
| `notify_stormguard_change(old, new, reason)` | `render_stormguard_change` | FSM state transition |
| `notify_reconnect(broker, attempt)` | `render_reconnect` | Broker reconnection |
| `notify_daily_report(sections)` | `render_daily_report` | End-of-day summary |
| `notify_heartbeat(...)` | `render_heartbeat` | Periodic health pulse |
| `notify_weekly_summary(...)` | `render_weekly_summary` | Weekly trading summary |
| `notify_session_phase(...)` | `render_session_phase` | Session governor transition |
| `notify_tca_pnl_supplement(...)` | `render_tca_pnl_supplement` | TCA + PnL follow-up |

## TelegramSender

```
TelegramSender(bot_token, chat_id, enabled=True)
  .send(message, critical=False)
```

- **Rate limiting**: Configurable (default 1 msg/sec). Critical messages bypass.
- **Retry**: Critical messages retry up to 3x with exponential backoff on transient errors (429, 500, 502, 503, 504).
- **Chunking**: Messages >4096 chars auto-split on newline boundaries.
- **Session**: `aiohttp.ClientSession` with 10s total timeout, auto-reconnection.

## TelegramCommandPoller

Polls Telegram `getUpdates` for interactive commands (separate from `bot/` module — this runs inside the trading engine container):

| Command | Action |
|---------|--------|
| `/stop` | Sets `hft:emergency_halt` flag in Redis → triggers HALT |
| `/status` | Replies "Status: running" |

- Chat ID whitelist validation
- Session reuse across poll cycles

## Alertmanager Bridge

Raw asyncio HTTP server (no framework dependency):

- `POST /webhook/alertmanager` — receives Alertmanager payload, formats alerts as HTML, forwards to Telegram
- `GET /healthz` — returns `200 OK`
- Port: `HFT_ALERT_BRIDGE_PORT` (default 8081)

## Integration Points

| Caller | Wired At | Events |
|--------|----------|--------|
| `bootstrap.py` | Startup | Creates `TelegramSender` + `NotificationDispatcher`, wires to subsystems |
| `RiskEngine` | `on_halt` callback | `notify_halt`, `notify_daily_loss` |
| `StormGuard` | State transition | `notify_stormguard_change` |
| `AutonomyMonitor` | Health signal | `notify_autonomy_transition` |
| `ReconciliationService` | Drift detected | `notify_recon_mismatch` |
| `DailyReportService` | End of day | `notify_daily_report`, `notify_tca_pnl_supplement` |
| `BackupManager` | Backup complete/fail | `notify_backup_status` |

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_TELEGRAM_BOT_TOKEN` | — | Telegram Bot API token |
| `HFT_TELEGRAM_CHAT_ID` | — | Target chat ID (also used for command whitelist) |
| `HFT_TELEGRAM_ENABLED` | `0` | `1` = enable Telegram sender at bootstrap |
| `HFT_WEBHOOK_URL` | — | Fallback webhook URL (critical events only) |
| `HFT_ALERT_BRIDGE_PORT` | `8081` | Alertmanager bridge HTTP port |
