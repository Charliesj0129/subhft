# Telegram Bot Service Design

## Goal

Wrap the existing Market Analysis Report Service (5-stage pipeline) into an interactive Telegram Bot that runs as a standalone Docker container, providing scheduled report delivery and on-demand query commands.

## Scope

**In scope (this spec):**
- Long-running Bot process using `python-telegram-bot` (polling mode)
- Scheduled report push: day session 13:50 CST, night session 05:05 CST
- Interactive commands: `/start`, `/report`, `/levels`, `/flow`, `/status`
- Independent Docker container (`hft-bot`) in existing docker-compose
- Single user (owner) only — no user management, no tier differentiation
- Refactor `run_pipeline()` to return rendered messages (prerequisite for bot reuse)
- Add lightweight query methods to `DataCollector` for `/levels` and `/flow`
- Add `python-telegram-bot` as optional dependency group

**Out of scope (future work):**
- User registration, subscription management, payment integration
- Webhook mode (polling is sufficient for single-user)
- Multi-symbol support (fixed to TXFD6)
- Price alert notifications (`/alert`)
- Natural language queries
- Free/paid tier rendering differentiation

## Prerequisite: Pipeline API Refactor

The current `run_pipeline()` in `reports/pipeline.py` sends messages directly via `Distributor` and returns `None`. The bot needs rendered messages returned to it so it can send them through its own transport.

**Required changes to `reports/pipeline.py`:**

1. Extract a new function `build_report(session, date, symbol) -> dict[str, list[str]]` that runs stages 1-4 (collect → signal → scenario → render) and returns the rendered message dict. This is pure computation with no side effects except ClickHouse reads.

2. Refactor `run_pipeline()` to call `build_report()` then `Distributor.send()`. Existing CLI behavior unchanged.

3. The bot calls `build_report()` directly, then sends messages through `python-telegram-bot`'s API. The existing `Distributor`/`ReportSender` are NOT used by the bot.

**Handling tick_count == 0:** `build_report()` returns `None` when no data exists. The bot handler checks for `None` and replies "該時段無交易資料".

## Prerequisite: Lightweight Collector Queries

The current `DataCollector.collect()` runs all 6 CH queries (including heavy spread/depth). `/levels` and `/flow` don't need all of them.

**Required changes to `reports/collector.py`:**

1. Add `collect_core(symbol, time_filter) -> SessionData` — runs only Q1 (OHLCV), Q2 (5m bars), Q3 (flow), Q4 (large trades). Skips Q5 (spread) and Q6 (depth). Returns `SessionData` with `spread_dist={}` and `depth_imbalance=[]`.

2. The existing `collect()` method calls `collect_core()` then adds Q5/Q6 on top (with existing OOM fallback). No behavior change.

3. `/levels` handler calls `collect_core()` → `SignalEngine.analyze()` → extracts S/R levels.

4. `/flow` handler calls `collect_core()` → extracts flow bars and large trade summary directly from `SessionData`.

## Architecture

### Principle

The Bot layer is a **trigger + transport layer** only. All analysis logic is 100% reused from `src/hft_platform/reports/`. No business logic lives in the bot module.

### Telegram Library Choice

Use `python-telegram-bot` (new dependency) rather than the existing `notifications/telegram.py` infrastructure. Rationale:
- Existing `TelegramSender` is fire-and-forget with rate-limit drops — suitable for alerts, not interactive bot
- Existing `TelegramCommandPoller` handles only `/stop` and `/status` — would need heavy modification
- `python-telegram-bot` provides: `CommandHandler` routing, `JobQueue` scheduling, `Update`/`Context` abstractions, built-in error handling, typed API
- Added as optional dependency group `[bot]` in `pyproject.toml` — does not affect existing platform

The existing `TelegramCommandPoller` in `notifications/telegram.py` remains unchanged for its emergency `/stop` role.

### Container Topology

```
docker-compose.yml
  hft-engine    ← trading runtime (unchanged)
  clickhouse    ← data store (unchanged)
  redis         ← cache (unchanged)
  hft-bot       ← NEW: Telegram Bot (polling, long-running)
```

`hft-bot` connects to ClickHouse for data. It does NOT connect to hft-engine or redis.

### Module Structure

```
src/hft_platform/bot/
├── __init__.py
├── __main__.py        # Entry: python -m hft_platform.bot
├── app.py             # BotApp: init Application, register handlers, start polling
├── handlers.py        # Command handlers: /start, /report, /levels, /flow, /status
└── scheduler.py       # JobQueue scheduled push functions
```

Each file < 200 lines. Total new code ~500 lines.

### Dependency Flow

```
bot/app.py
  → bot/handlers.py    (command routing)
  → bot/scheduler.py   (scheduled jobs)
  → reports/pipeline.py::build_report()  (stages 1-4, returns rendered dict)
  → reports/collector.py::collect_core()  (lightweight queries for /levels, /flow)
  → reports/signals.py::SignalEngine      (for /levels)
```

Bot modules import from `reports/` — never the reverse.

## Access Control

Single-user enforcement: all command handlers check `update.effective_chat.id == int(HFT_TELEGRAM_CHAT_ID)`. If mismatch, reply "未授權" and return. This is checked in a shared decorator or middleware, not repeated in each handler.

**Acceptance criteria:**
- Messages from non-owner chat IDs receive "未授權" reply
- Non-owner messages are logged at WARNING level with the rejected chat_id
- Scheduled push jobs only send to the configured owner chat_id
- Unit test: handler called with wrong chat_id → returns "未授權", no pipeline execution

## Commands

### `/start`

Welcome message with available commands list. Sent once on first interaction.

Response:
```
HFT 市場分析 Bot

可用指令：
/report [day|night] — 取得完整分析報告
/levels — 當前支撐壓力位
/flow — 最新流向摘要
/status — Bot 運行狀態
```

### `/report [day|night]`

Calls `build_report(session, date)` which runs stages 1-4 and returns the rendered message dict. The bot handler sends each message via `context.bot.send_message()`.

If no argument given, auto-detect session based on current time (same logic as `reports/pipeline.py::resolve_trading_date()`).

- Sends a "產生報告中..." placeholder first
- Calls `build_report()` — returns `dict[str, list[str]]` or `None`
- If `None`: replies "該時段無交易資料"
- Otherwise: sends each message in `rendered["paid"]` sequentially with 1.5s delay
- On exception: sends error summary to user, logs full traceback

### `/levels`

Calls `DataCollector().collect_core()` (Q1-Q4 only, skips spread/depth) then `SignalEngine.analyze()` to extract support/resistance levels.

Response format:
```
支撐壓力位 (TXFD6 日盤 2026-03-28)

壓力：
  R1: 20,150 ★★★ 大單賣 40口
  R2: 20,500 ★★ 整數關卡

支撐：
  S1: 19,800 ★★★ 雙底
  S2: 19,500 ★★ 整數關卡
```

### `/flow`

Calls `DataCollector().collect_core()` (Q1-Q4 only) and extracts flow data directly from `SessionData.flow_5m` and `SessionData.large_trades`.

Response format:
```
流向摘要 (TXFD6 日盤 2026-03-28)

U/D Ratio: 0.93 (偏空)
成交量: 85,432
大單: 買 12 筆 / 賣 18 筆

最近 5 根 K棒流向：
09:00-09:05 ▼ 0.85
09:05-09:10 ▲ 1.12
09:10-09:15 ▼ 0.78
09:15-09:20 ▼ 0.91
09:20-09:25 ▲ 1.05
```

### `/status`

Bot health check. No ClickHouse query — uses in-memory state only.

Tracks:
- `_start_time`: set at bot init
- `_last_day_report`: updated after each successful day report push
- `_last_night_report`: updated after each successful night report push
- `_last_ch_ok`: updated on every successful CH query (piggybacked from `/report`, `/levels`, `/flow`)

Response format:
```
Bot 狀態

運行時間: 3h 42m
上次日盤報告: 2026-03-28 13:50 CST
上次夜盤報告: 2026-03-28 05:05 CST
ClickHouse: 最後成功 12 分鐘前
```

If no CH query has ever succeeded, show "ClickHouse: 尚未連線".

## Scheduled Push

Uses `python-telegram-bot` built-in `JobQueue` (APScheduler under the hood).

### Schedule

| Job | Time (CST) | Days | Trigger | Description |
|-----|-----------|------|---------|-------------|
| Day report | 13:50 | Mon-Fri | `job_queue.run_daily` | Full pipeline, day session, date=today |
| Night report | 05:05 | Mon-Sat | `job_queue.run_daily` | Full pipeline, night session, date from `resolve_trading_date("night")` |

### Holiday and Schedule Logic

Night session opens at 15:00 and closes at 05:00 next morning. The 05:05 job runs Mon-Sat (not Tue-Sat) because:
- Friday 15:00 → Saturday 05:00 is a valid night session. The Saturday 05:05 job catches it.
- Saturday 15:00 → Sunday 05:00 does NOT exist (no trading). The Sunday job is skipped (Sunday not in schedule).
- Sunday 15:00 → Monday 05:00 does NOT exist. The Monday 05:05 job would find no data.

When `build_report()` returns `None` (no data, e.g. market holiday), the scheduled job logs a warning and does nothing — no message sent.

Taiwan market holidays beyond weekends are not handled in MVP. The "no data" path handles them gracefully.

### Push Target

`HFT_TELEGRAM_CHAT_ID` environment variable (owner's chat ID).

## Docker Integration

### docker-compose.yml Addition

The `hft-bot` service does NOT use `<<: *hft-common` because that anchor includes volumes, healthcheck, and resource settings intended for the trading engine. Instead, it declares only the environment variables it needs.

```yaml
hft-bot:
  build: .
  command: ["python", "-m", "hft_platform.bot"]
  environment:
    HFT_TELEGRAM_BOT_TOKEN: ${HFT_TELEGRAM_BOT_TOKEN}
    HFT_TELEGRAM_CHAT_ID: ${HFT_TELEGRAM_CHAT_ID}
    HFT_CLICKHOUSE_HOST: clickhouse
    HFT_CLICKHOUSE_USER: ${CLICKHOUSE_USER:-default}
    HFT_CLICKHOUSE_PASSWORD: ${CLICKHOUSE_PASSWORD:-}
    HFT_BOT_SYMBOL: ${HFT_BOT_SYMBOL:-TXFD6}
    TZ: Asia/Taipei
  depends_on:
    clickhouse:
      condition: service_healthy
  restart: unless-stopped
  mem_limit: 512m
  logging:
    driver: json-file
    options:
      max-size: "50m"
      max-file: "3"
```

### Health Signal

No HTTP health endpoint. The bot logs:
- `bot.started` on successful polling init
- `bot.heartbeat` every 5 minutes via a recurring JobQueue job (includes uptime and last report timestamps)

Docker `restart: unless-stopped` handles process crashes. `docker compose logs hft-bot` for debugging.

## Error Handling

| Scenario | Bot Response | Logging |
|----------|-------------|---------|
| CH connection failed | "資料庫暫時不可用，請稍後再試" | `bot.ch_error` with exception |
| Pipeline stage failed | "報告產生失敗：{stage} 錯誤" | `bot.pipeline_error` with full traceback |
| No data for session | "該時段無交易資料" | `bot.no_data` at INFO level |
| Telegram API error | Retry via `python-telegram-bot` built-in mechanism | `bot.send_error` with status code |
| Unauthorized user | "未授權" | `bot.unauthorized` at WARNING with chat_id |
| Unhandled exception | "內部錯誤，已記錄" | `bot.unhandled` with traceback |

All logging via `structlog`, consistent with platform conventions.

## Environment Variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `HFT_TELEGRAM_BOT_TOKEN` | Yes | — | Telegram Bot API token |
| `HFT_TELEGRAM_CHAT_ID` | Yes | — | Owner's chat ID for push + access control |
| `HFT_CLICKHOUSE_HOST` | No | `localhost` | ClickHouse host |
| `HFT_CLICKHOUSE_USER` | No | `default` | ClickHouse user |
| `HFT_CLICKHOUSE_PASSWORD` | No | — | ClickHouse password |
| `HFT_BOT_SYMBOL` | No | `TXFD6` | Default symbol for queries |

## Dependency Addition

Add `python-telegram-bot` as an optional dependency group in `pyproject.toml`:

```toml
[project.optional-dependencies]
bot = ["python-telegram-bot[job-queue]>=21.0"]
```

The `[job-queue]` extra includes APScheduler for `JobQueue` support. Docker image installs with `pip install -e ".[bot]"`.

## Testing Strategy

- **Unit tests**: Command handlers with mocked `Update`/`Context` objects
  - `/report` with mocked `build_report()` returning rendered dict
  - `/report` with mocked `build_report()` returning `None` (no data)
  - `/levels` with mocked `collect_core()` + `SignalEngine`
  - `/flow` with mocked `collect_core()`
  - `/status` returns uptime and last-report timestamps
  - Unauthorized chat_id → "未授權" reply, no pipeline execution
- **Unit tests**: Scheduler job functions with mocked `build_report()`
  - Successful push updates last-report timestamp
  - No-data session logs warning, sends nothing
- **Integration test**: Full `/report` command flow with mocked ClickHouse
- No E2E tests against live Telegram API (manual verification)

## Future Evolution Path

1. **User management**: Add SQLite DB for chat_id → tier mapping, replace decorator whitelist with DB lookup
2. **Tier differentiation**: Free users get `/report` summary only, paid get full 5 messages
3. **Multi-symbol**: `/report TMFD6`, `/levels MXFD6` — pass symbol through to `build_report()`
4. **Alerts**: `/alert 20000` price notification via FeatureEngine subscription
5. **Payment integration**: Auto-subscribe after payment confirmation
6. **Webhook mode**: Switch from polling when scale demands it
