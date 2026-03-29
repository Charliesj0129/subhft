# Telegram Analysis Product — Design Spec

**Date**: 2026-03-29
**Status**: Approved
**Scope**: Phase 1 (self-use) + Phase 2 (paid subscription) roadmap

## 1. Overview

Package the HFT platform's existing reports pipeline and Telegram bot skeleton into a
two-phase product:

- **Phase 1**: Self-use — stable, multi-symbol, automated daily analysis delivered via
  Telegram bot (owner channel only).
- **Phase 2**: Paid subscription — add a public free channel (summary) and a private paid
  channel (full analysis). Member management is manual.

## 2. Architecture

```
                ┌─────────────────────────────────────┐
                │         Telegram Bot Process         │
                │  (python-telegram-bot polling loop)  │
                ├──────────────────┬──────────────────┤
                │  scheduler.py    │  handlers.py      │
                │  13:50 day push  │  /start            │
                │  05:05 night push│  /report [symbol]  │
                │                  │  /levels [symbol]  │
                │                  │  /flow [symbol]    │
                │                  │  /status           │
                └────────┬─────────┼────────┬──────────┘
                         │         │        │
                         ▼         │        ▼
                ┌──────────────────────────────────────┐
                │     reports/pipeline.py               │
                │  build_report(symbol, session)        │
                │  collect_core(symbol)                 │
                └──────────────────┬───────────────────┘
                                   │
                ┌──────────────────▼───────────────────┐
                │  reports/distributor.py               │
                │  owner_channel  (Phase 1)            │
                │  free_channel   (Phase 2)            │
                │  paid_channel   (Phase 2)            │
                └──────────────────────────────────────┘
```

**Key decisions:**

- Single process. Scheduler and handlers share one event loop.
- `build_report()` is the single entry point for both scheduled push and on-demand
  commands.
- Multi-symbol support is a loop at the scheduler/handler layer; pipeline internals are
  unchanged.
- Phase 1 → Phase 2 requires env var changes **plus** migrating the send path in
  `handlers.py` and `scheduler.py` from direct `context.bot.send_message()` to
  `Distributor.send()`. See Section 5 for details.

## 3. Multi-Symbol Report Mechanism

### Configuration

```bash
# Replaces the existing HFT_BOT_SYMBOL (single symbol) with a comma-separated list.
HFT_REPORT_SYMBOLS=TXFD6,TMFD6,2330
```

### Symbol List Parsing

Responsibility lives in a single shared helper (e.g., `bot/_symbols.py` or top of
`app.py`):

```python
def get_report_symbols() -> list[str]:
    raw = os.environ.get("HFT_REPORT_SYMBOLS", "TXFD6")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if not symbols:
        symbols = ["TXFD6"]
    return symbols
```

- **Default**: `["TXFD6"]` when env var is absent or empty.
- **Ordering**: config order is push order (user controls priority).
- **Validation**: invalid symbols produce `build_report() → None` (no tick data),
  logged as warning, not fatal.
- **Used by**: `scheduler.py` (push loop), `handlers.py` (default symbol for commands).

### Behaviour

- Scheduler iterates over `get_report_symbols()`, calling
  `build_report(symbol, session)` for each.
- Each symbol produces an independent report (no cross-symbol aggregation).
- 1.5-second sleep between sends to respect Telegram rate limits (matches existing
  `ReportSender.send_batch()` default).

### Per-Symbol Adjustments

- `collector.py` already filters by `symbol` parameter — no changes needed.
- `signals.py` rules (IF-01 through IF-06, SR-02, SR-06) are symbol-agnostic.
- Large-trade threshold mapping needs per-symbol entries (existing: TXFD6=10, TMFD6=30).
  Add equities thresholds (e.g., 2330=100).

### What Does NOT Change

- Pipeline internals (collector → signals → scenarios → renderer).
- `build_report()` signature adds `symbol` parameter only.
- Renderer free/paid tier logic.

## 4. Bot Module Design

### 4.1 handlers.py — Command Handlers

| Command                          | Behaviour                                             | Data Source                       |
| -------------------------------- | ----------------------------------------------------- | --------------------------------- |
| `/start`                         | Welcome message + command list                        | Static text                       |
| `/report [symbol] [day\|night]`  | Full analysis report                                  | `build_report()`                  |
| `/levels [symbol]`               | Support/resistance price levels                       | `collect_core()` → SR rules       |
| `/flow [symbol]`                 | Flow summary (large trades, uptick/downtick)          | `collect_core()` → IF rules       |
| `/status`                        | Bot health (uptime, last push time, CH connection)    | In-memory state from `app.py`     |

- All commands pass through `owner_only()` decorator (from `app.py`).
- `/report` grammar: `/report [symbol] [day|night]`. Both args optional, positional.
  - No args: first symbol in `HFT_REPORT_SYMBOLS`, session auto-detected by time of day.
  - One arg: if it matches `day`/`night`, treat as session; otherwise treat as symbol.
  - Two args: first = symbol, second = session.
  - Examples: `/report`, `/report 2330`, `/report night`, `/report TMFD6 night`.
- `/report` takes 3-5 seconds (ClickHouse query). Send a "generating..." placeholder
  first, then send the full report as 3 (free) or 5 (paid) sequential messages.
  The placeholder is kept as-is (not edited), followed by the report messages.

### 4.2 scheduler.py — Scheduled Push

```
Day session close:   13:50 CST → for symbol in symbols: build_report(session="day")
Night session close: 05:05 CST → for symbol in symbols: build_report(session="night")
```

- Uses `python-telegram-bot` `JobQueue` (APScheduler backend).
- Job failure does not crash the bot. Logs via structlog, updates state timestamps.
- After push, updates `last_day_report` / `last_night_report` in `app.py` state.

### 4.3 Error Handling

| Scenario              | Handling                                           |
| --------------------- | -------------------------------------------------- |
| ClickHouse unreachable | Reply "database temporarily unavailable", no crash |
| Symbol has no data     | `build_report()` returns None, skip that symbol    |
| Telegram API 429       | Distributor retry + Retry-After (existing logic)   |
| Bad command format     | Reply with usage hint                              |

## 5. Phase 2 — Paid Subscription Extension

### Channel Configuration

```bash
# Phase 1 (self-use)
HFT_TELEGRAM_CHAT_ID=<owner>

# Phase 2 (add these)
HFT_REPORT_FREE_CHANNEL_ID=<public_channel>
HFT_REPORT_FREE_ENABLED=1
HFT_REPORT_PAID_CHANNEL_ID=<private_channel>
HFT_REPORT_PAID_ENABLED=1
```

- `Distributor` already has free/paid routing logic (`load_channels()` + tier-based
  dispatch). However, the current bot code (`handlers.py`, `scheduler.py`) bypasses
  `Distributor` entirely — it sends `rendered["paid"]` directly via
  `context.bot.send_message()` to the owner chat.
- **Phase 2 migration required**: Refactor both `handlers.py` (on-demand) and
  `scheduler.py` (scheduled push) to route through `Distributor.send(rendered)` instead
  of direct sends. This ensures free/paid channel routing works automatically.
  - Phase 1 can keep direct sends (owner-only, always "paid" tier).
  - Phase 2 implementation MUST switch to Distributor before enabling free/paid channels.
  - Estimated: ~20 LOC change per file (replace send loop with Distributor call).
- Member management: manual invite/remove via Telegram.

### Free vs Paid Content

| Content                                 | Free | Paid |
| --------------------------------------- | ---- | ---- |
| Daily summary (OHLCV, % change)        | yes  | yes  |
| Flow direction (bullish/bearish/neutral)| yes  | yes  |
| Support/resistance levels               | no   | yes  |
| Large trade analysis + cluster detection| no   | yes  |
| Scenario planning (entry/target/stop)   | no   | yes  |
| Confidence score                        | no   | yes  |

- Renderer already produces 3 messages (free) vs 5 messages (paid). No code changes.

### Phase 2 Exclusions

- No automated payment / billing integration.
- No user management database.
- No web interface.
- No personalized reports (all paid users see the same report).

## 6. Deployment

### Docker Compose Service

```yaml
hft-bot:
  build: .
  command: python -m hft_platform.bot
  environment:
    - HFT_TELEGRAM_BOT_TOKEN=${HFT_TELEGRAM_BOT_TOKEN}
    - HFT_TELEGRAM_CHAT_ID=${HFT_TELEGRAM_CHAT_ID}
    - HFT_REPORT_SYMBOLS=TXFD6,TMFD6,2330
    - HFT_CLICKHOUSE_HOST=clickhouse
  depends_on:
    clickhouse:
      condition: service_healthy
  restart: unless-stopped
  mem_limit: 256m
  cpus: 0.5
```

- Independent container from `hft-engine`.
- Only requires ClickHouse connectivity. No Redis, no broker SDK.
- Resource: 256 MB RAM, 0.5 CPU.

### Monitoring

| Metric               | Method                                              |
| -------------------- | --------------------------------------------------- |
| Bot alive            | `/status` command reports uptime                    |
| Push success         | structlog records each push result                  |
| CH connectivity      | `last_ch_ok` timestamp in app state                 |
| Report generation fail| structlog error + `/status` shows last success time |

- No Prometheus metrics for bot (load too low to justify).
- Docker `restart: unless-stopped` handles crash recovery.

### Deployment Exclusions

- No health-check HTTP endpoint.
- No Grafana dashboard for bot.
- No log aggregation (structlog → stdout → `docker logs`).

## 7. File Changes Summary

### New Files

| File                              | Purpose                                          |
| --------------------------------- | ------------------------------------------------ |
| `tests/unit/test_bot_handlers.py` | Handler unit tests (expand existing if present)  |
| `tests/unit/test_bot_scheduler.py`| Scheduler unit tests (expand existing if present)|

### Modified Files (Phase 1)

| File                                  | Change                                                        |
| ------------------------------------- | ------------------------------------------------------------- |
| `src/hft_platform/bot/handlers.py`    | Add symbol+session arg parsing, multi-symbol default, /report grammar |
| `src/hft_platform/bot/scheduler.py`   | Replace single `_get_symbol()` with `get_report_symbols()` loop |
| `src/hft_platform/bot/app.py`         | Add `get_report_symbols()` helper or shared symbol config     |
| `src/hft_platform/reports/collector.py`| Add equity large-trade thresholds (e.g., 2330)               |
| `docker-compose.yml`                  | Add `hft-bot` service                                        |

### Modified Files (Phase 2 — future)

| File                                  | Change                                                        |
| ------------------------------------- | ------------------------------------------------------------- |
| `src/hft_platform/bot/handlers.py`    | Replace direct `context.bot.send_message()` with `Distributor.send()` |
| `src/hft_platform/bot/scheduler.py`   | Replace direct `context.bot.send_message()` with `Distributor.send()` |

### Unchanged

- `reports/pipeline.py` — `build_report()` already accepts `symbol` parameter.
- `reports/signals.py` — rules are symbol-agnostic.
- `reports/scenarios.py` — derives from signal output, symbol-independent.
- `reports/renderer.py` — free/paid tier logic already complete.
- `reports/distributor.py` — free/paid channel routing already complete.
- `reports/models.py` — data contracts are generic.
- `notifications/` — existing alert system remains separate.
