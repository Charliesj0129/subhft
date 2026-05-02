# bot — Telegram Bot

> **Package**: `src/hft_platform/bot/`
> **Files**: 5 (`app.py`, `handlers.py`, `scheduler.py`, `__init__.py`, `__main__.py`)
> **Runtime Plane**: Cold (standalone Docker service, not in trading pipeline)
> **Dependency**: `python-telegram-bot[job-queue]>=21.0` (optional install group `bot`)

## Overview

Standalone Telegram Bot service for interactive market analysis and scheduled report delivery. Runs as a separate Docker container (`hft-bot`), queries ClickHouse for data, and uses the `reports/` pipeline for analysis. Owner-only access control via `HFT_TELEGRAM_CHAT_ID`.

This module is the **user-facing interface** — it composes commands on top of `reports/` (analysis) and `notifications/` (delivery infrastructure).

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `app.py` | `create_app()`, `owner_only`, `get_report_symbols()`, `LatestReportContext` | Application factory, access control decorator, shared state |
| `handlers.py` | `cmd_report`, `cmd_report_rule`, `cmd_ask`, `cmd_levels`, `cmd_flow`, `cmd_status`, `cmd_start` | 7 command handler implementations |
| `scheduler.py` | `schedule_jobs()` | Scheduled push jobs (day/night report, heartbeat) |
| `__main__.py` | — | Entry point: `python -m hft_platform.bot` |

## Commands

| Command | Arguments | Description |
|---------|-----------|-------------|
| `/start` | — | Welcome message with available commands |
| `/report` | `[symbol] [day\|night]` | Hybrid LLM report (facts + LLM reasoning + composition) |
| `/report_rule` | `[symbol] [day\|night]` | Rule-based report (no LLM, deterministic reasoning) |
| `/ask` | `<question>` | Follow-up Q&A on the last `/report` result (uses cached `LatestReportContext`) |
| `/levels` | `[symbol]` | Support/resistance levels from 5-day lookback (via `LevelReasoner`) |
| `/flow` | `[symbol]` | Flow summary: U/D ratio, net flow, strongest buy/sell bars, segment breakdown |
| `/status` | — | Bot health: uptime, last report timestamps, ClickHouse connectivity |

Arguments default: symbol = first in `HFT_REPORT_SYMBOLS` (default `TXFD6`); session = auto-detected by current hour (day if 07:00-15:00 CST).

## Scheduled Push

| Job | Time (CST) | Days | Behavior |
|-----|------------|------|----------|
| Day report | 13:50 | Mon–Fri | Pushes hybrid report for all configured symbols |
| Night report | 05:05 | Mon–Sat | Pushes hybrid report for all configured symbols |
| Heartbeat | Every 5 min | All | Logs uptime + last report timestamps |

No-data handling: skips silently (warning log, no message sent).

Multi-symbol: iterates `HFT_REPORT_SYMBOLS` (comma-separated), sends each symbol's report sequentially with 1.5s pacing between message parts.

## Access Control

- `@owner_only` decorator on all command handlers
- Compares `update.effective_chat.id` against `HFT_TELEGRAM_CHAT_ID`
- Unauthorized requests receive "未授權" reply

## Architecture

```
User (Telegram)
  → python-telegram-bot polling
    → handlers.py (command dispatch)
      → reports/pipeline.py (build_hybrid_report_async / build_report)
        → reports/collector.py (ClickHouse queries)
        → reports/facts.py (fact extraction)
        → reports/reasoner.py (rule-based) / reports/llm_reasoner.py (LLM)
        → reports/composer.py (HTML + image composition)
      → Telegram Bot API (send_message / send_photo)
```

## Docker

```yaml
# docker-compose.yml
hft-bot:
  command: python -m hft_platform.bot
  environment:
    - HFT_TELEGRAM_BOT_TOKEN=${HFT_TELEGRAM_BOT_TOKEN}
    - HFT_TELEGRAM_CHAT_ID=${HFT_TELEGRAM_CHAT_ID}
    - HFT_REPORT_SYMBOLS=TXFD6
  depends_on: [clickhouse]
  deploy:
    resources:
      limits: { memory: 512M }
```

Also starts a background `/healthz` HTTP server for container liveness probes.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_TELEGRAM_BOT_TOKEN` | — | Bot API token (from BotFather) |
| `HFT_TELEGRAM_CHAT_ID` | — | Owner chat ID for access control |
| `HFT_REPORT_SYMBOLS` | `TXFD6` | Comma-separated symbol list |
