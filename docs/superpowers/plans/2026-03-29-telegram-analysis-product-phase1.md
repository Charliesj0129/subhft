# Telegram Analysis Product — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing bot skeleton + reports pipeline into a working multi-symbol Telegram bot that pushes daily analysis reports to the owner.

**Architecture:** Single-process bot using `python-telegram-bot` polling. Scheduled jobs iterate over a configurable symbol list, calling the existing `build_report()` pipeline per symbol. Interactive commands (`/report`, `/levels`, `/flow`) accept optional symbol + session args. All sends go directly to the owner chat (Phase 1 — no Distributor integration yet).

**Tech Stack:** Python 3.12, python-telegram-bot 21+, ClickHouse (via existing reports pipeline), structlog, pytest

**Spec:** `docs/superpowers/specs/2026-03-29-telegram-analysis-product-design.md`

---

### Task 1: Add `get_report_symbols()` helper to `app.py`

**Files:**
- Modify: `src/hft_platform/bot/app.py`
- Test: `tests/unit/test_bot_handlers.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_bot_handlers.py` at the top level (outside existing classes):

```python
class TestGetReportSymbols:
    def test_default_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_REPORT_SYMBOLS", raising=False)
        from hft_platform.bot.app import get_report_symbols

        assert get_report_symbols() == ["TXFD6"]

    def test_parses_comma_separated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "TXFD6,TMFD6,2330")
        from hft_platform.bot.app import get_report_symbols

        assert get_report_symbols() == ["TXFD6", "TMFD6", "2330"]

    def test_strips_whitespace_and_uppercases(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", " txfd6 , tmfd6 ")
        from hft_platform.bot.app import get_report_symbols

        assert get_report_symbols() == ["TXFD6", "TMFD6"]

    def test_empty_string_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "")
        from hft_platform.bot.app import get_report_symbols

        assert get_report_symbols() == ["TXFD6"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bot_handlers.py::TestGetReportSymbols -v`
Expected: FAIL — `ImportError: cannot import name 'get_report_symbols'`

- [ ] **Step 3: Write implementation**

Add to `src/hft_platform/bot/app.py` after the `owner_only` function (before the Application factory section):

```python
# ---------------------------------------------------------------------------
# Symbol configuration
# ---------------------------------------------------------------------------


def get_report_symbols() -> list[str]:
    """Return the list of symbols to include in reports.

    Reads ``HFT_REPORT_SYMBOLS`` (comma-separated). Falls back to
    ``["TXFD6"]`` when absent or empty.
    """
    raw = os.environ.get("HFT_REPORT_SYMBOLS", "TXFD6")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if not symbols:
        symbols = ["TXFD6"]
    return symbols
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bot_handlers.py::TestGetReportSymbols -v`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/bot/app.py tests/unit/test_bot_handlers.py
git commit -m "feat(bot): add get_report_symbols() multi-symbol config helper"
```

---

### Task 2: Update `handlers.py` — `/report [symbol] [day|night]` arg parsing

**Files:**
- Modify: `src/hft_platform/bot/handlers.py`
- Test: `tests/unit/test_bot_handlers.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_bot_handlers.py`:

```python
class TestReportArgParsing:
    """Test /report [symbol] [day|night] positional arg parsing."""

    @pytest.mark.asyncio
    async def test_no_args_uses_default_symbol_and_auto_session(self) -> None:
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report")
        ctx = _make_context()
        ctx.args = []
        with (
            patch("hft_platform.reports.pipeline.build_report") as mock_build,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
            patch("hft_platform.bot.app.get_report_symbols", return_value=["TXFD6", "TMFD6"]),
        ):
            mock_build.return_value = {"paid": ["msg1"], "free": ["fmsg"]}
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)
            # Should use first symbol from list (TXFD6)
            call_args = mock_build.call_args
            assert call_args[1].get("symbol", call_args[0][2] if len(call_args[0]) > 2 else None) == "TXFD6" or \
                   call_args.kwargs.get("symbol") == "TXFD6" or \
                   "TXFD6" in str(call_args)

    @pytest.mark.asyncio
    async def test_symbol_only_arg(self) -> None:
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report 2330")
        ctx = _make_context()
        ctx.args = ["2330"]
        with (
            patch("hft_platform.reports.pipeline.build_report") as mock_build,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_build.return_value = {"paid": ["msg1"], "free": ["fmsg"]}
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)
            assert "2330" in str(mock_build.call_args)

    @pytest.mark.asyncio
    async def test_session_only_arg(self) -> None:
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report night")
        ctx = _make_context()
        ctx.args = ["night"]
        with (
            patch("hft_platform.reports.pipeline.build_report") as mock_build,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_build.return_value = {"paid": ["msg1"], "free": ["fmsg"]}
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)
            # Session arg should be "night"
            call_str = str(mock_build.call_args)
            assert "night" in call_str

    @pytest.mark.asyncio
    async def test_symbol_and_session_args(self) -> None:
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report TMFD6 night")
        ctx = _make_context()
        ctx.args = ["TMFD6", "night"]
        with (
            patch("hft_platform.reports.pipeline.build_report") as mock_build,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_build.return_value = {"paid": ["msg1"], "free": ["fmsg"]}
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)
            call_str = str(mock_build.call_args)
            assert "TMFD6" in call_str
            assert "night" in call_str
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot_handlers.py::TestReportArgParsing -v`
Expected: FAIL — current `cmd_report` doesn't handle symbol arg

- [ ] **Step 3: Rewrite `cmd_report` in `handlers.py`**

Replace the existing `cmd_report` function with:

```python
def _parse_report_args(args: list[str]) -> tuple[str, str | None]:
    """Parse /report [symbol] [day|night] positional args.

    Returns (symbol, session_or_none).
    - No args: (default_symbol, None)
    - One arg: if 'day'/'night' → (default_symbol, session); else → (arg, None)
    - Two args: (symbol, session)
    """
    from hft_platform.bot.app import get_report_symbols

    default_symbol = get_report_symbols()[0]
    sessions = {"day", "night"}

    if not args:
        return default_symbol, None
    if len(args) == 1:
        if args[0].lower() in sessions:
            return default_symbol, args[0].lower()
        return args[0].upper(), None
    # Two or more args: first = symbol, second = session
    symbol = args[0].upper()
    session = args[1].lower() if args[1].lower() in sessions else None
    return symbol, session


@owner_only
async def cmd_report(update: Any, context: Any) -> None:
    """Handle /report [symbol] [day|night] command."""
    import hft_platform.bot.app as bot_app
    from hft_platform.reports.pipeline import build_report, resolve_trading_date

    symbol, session = _parse_report_args(context.args or [])
    if session is None:
        now = datetime.now(_TZ)
        session = "day" if 7 <= now.hour < 15 else "night"

    date = resolve_trading_date(session)
    await update.message.reply_text(f"產生報告中... ({symbol} {session} {date})")

    try:
        rendered = build_report(session, date, symbol)
        bot_app.last_ch_ok = datetime.now(_TZ)
    except Exception as exc:
        _log.error("bot.report_error", exc=str(exc), exc_info=True)
        await update.message.reply_text(f"報告產生失敗：{exc}")
        return

    if rendered is None:
        await update.message.reply_text("該時段無交易資料")
        return

    chat_id = update.effective_chat.id
    for msg in rendered["paid"]:
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        await asyncio.sleep(1.5)

    if session == "day":
        bot_app.last_day_report = datetime.now(_TZ)
    else:
        bot_app.last_night_report = datetime.now(_TZ)
```

Also remove the old `_get_symbol()` function from `handlers.py` (lines 22-23).

- [ ] **Step 4: Run all handler tests**

Run: `uv run pytest tests/unit/test_bot_handlers.py -v`
Expected: ALL PASSED (existing tests + new arg parsing tests)

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/bot/handlers.py tests/unit/test_bot_handlers.py
git commit -m "feat(bot): add /report [symbol] [day|night] arg parsing"
```

---

### Task 3: Update `handlers.py` — multi-symbol `/levels` and `/flow`

**Files:**
- Modify: `src/hft_platform/bot/handlers.py`
- Test: `tests/unit/test_bot_handlers.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_bot_handlers.py`:

```python
class TestLevelsWithSymbol:
    @pytest.mark.asyncio
    async def test_levels_with_explicit_symbol(self) -> None:
        from hft_platform.bot.handlers import cmd_levels

        update = _make_update(chat_id=12345, text="/levels 2330")
        ctx = _make_context()
        ctx.args = ["2330"]
        fake_sd = MagicMock()
        fake_sd.tick_count = 100
        fake_sd.session = "day"
        fake_sd.date = "2026-03-28"
        fake_sd.symbol = "2330"
        fake_signal = MagicMock()
        fake_signal.supports = []
        fake_signal.resistances = []
        with (
            patch("hft_platform.bot.handlers._collect_core_for_latest") as mock_collect,
            patch("hft_platform.reports.signals.SignalEngine") as MockSignal,
        ):
            mock_collect.return_value = fake_sd
            MockSignal.return_value.analyze.return_value = fake_signal
            await cmd_levels(update, ctx)
        # Verify collect was called with symbol="2330"
        mock_collect.assert_called_once_with("2330")


class TestFlowWithSymbol:
    @pytest.mark.asyncio
    async def test_flow_with_explicit_symbol(self) -> None:
        from hft_platform.bot.handlers import cmd_flow

        update = _make_update(chat_id=12345, text="/flow TMFD6")
        ctx = _make_context()
        ctx.args = ["TMFD6"]
        fake_sd = MagicMock()
        fake_sd.tick_count = 100
        fake_sd.session = "day"
        fake_sd.date = "2026-03-28"
        fake_sd.symbol = "TMFD6"
        fake_sd.volume = 30000
        fake_sd.flow_5m = []
        fake_sd.large_trades = []
        with patch("hft_platform.bot.handlers._collect_core_for_latest") as mock_collect:
            mock_collect.return_value = fake_sd
            await cmd_flow(update, ctx)
        mock_collect.assert_called_once_with("TMFD6")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot_handlers.py::TestLevelsWithSymbol tests/unit/test_bot_handlers.py::TestFlowWithSymbol -v`
Expected: FAIL — `_collect_core_for_latest` doesn't accept a symbol arg

- [ ] **Step 3: Update `_collect_core_for_latest`, `cmd_levels`, `cmd_flow`**

In `src/hft_platform/bot/handlers.py`, replace the `_collect_core_for_latest` function:

```python
def _collect_core_for_latest(symbol: str | None = None) -> Any:
    """Run collect_core() for the most recent session."""
    from hft_platform.reports.collector import DataCollector, _day_filter, _night_filter
    from hft_platform.reports.pipeline import resolve_trading_date

    from hft_platform.bot.app import get_report_symbols

    now = datetime.now(_TZ)
    if symbol is None:
        symbol = get_report_symbols()[0]

    for session in ("day", "night"):
        date = resolve_trading_date(session, now=now)
        time_filter = _day_filter(date) if session == "day" else _night_filter(date)
        collector = DataCollector()
        sd = collector.collect_core(symbol, time_filter, session=session, date=date)
        if sd.tick_count > 0:
            return sd

    return sd  # Return last attempt even if empty
```

Update `cmd_levels` to parse symbol from args (add after the decorator, before the try block):

```python
@owner_only
async def cmd_levels(update: Any, context: Any) -> None:
    """Handle /levels [symbol] command."""
    import hft_platform.bot.app as bot_app
    from hft_platform.reports.signals import SignalEngine

    args = context.args or []
    symbol = args[0].upper() if args else None

    try:
        sd = _collect_core_for_latest(symbol)
        bot_app.last_ch_ok = datetime.now(_TZ)
    # ... rest unchanged
```

Update `cmd_flow` similarly:

```python
@owner_only
async def cmd_flow(update: Any, context: Any) -> None:
    """Handle /flow [symbol] command."""
    import hft_platform.bot.app as bot_app

    args = context.args or []
    symbol = args[0].upper() if args else None

    try:
        sd = _collect_core_for_latest(symbol)
        bot_app.last_ch_ok = datetime.now(_TZ)
    # ... rest unchanged
```

- [ ] **Step 4: Run all handler tests**

Run: `uv run pytest tests/unit/test_bot_handlers.py -v`
Expected: ALL PASSED

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/bot/handlers.py tests/unit/test_bot_handlers.py
git commit -m "feat(bot): add symbol arg to /levels and /flow commands"
```

---

### Task 4: Update `scheduler.py` — multi-symbol push loop

**Files:**
- Modify: `src/hft_platform/bot/scheduler.py`
- Test: `tests/unit/test_bot_scheduler.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_bot_scheduler.py`:

```python
class TestMultiSymbolPush:
    @pytest.mark.asyncio
    async def test_push_iterates_over_all_symbols(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "TXFD6,TMFD6,2330")
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()

        with (
            patch("hft_platform.reports.pipeline.build_report") as mock_build,
            patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio,
        ):
            mock_build.return_value = {"paid": ["msg1"], "free": ["fmsg"]}
            mock_asyncio.sleep = AsyncMock()
            await _push_report(ctx, "day")

        # build_report should be called 3 times (one per symbol)
        assert mock_build.call_count == 3
        symbols_called = [call[0][2] for call in mock_build.call_args_list]  # 3rd positional arg
        assert symbols_called == ["TXFD6", "TMFD6", "2330"]

    @pytest.mark.asyncio
    async def test_push_skips_symbol_with_no_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "TXFD6,NOSYMBOL")
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()

        def side_effect(session, date, symbol):
            if symbol == "NOSYMBOL":
                return None
            return {"paid": ["msg1"], "free": ["fmsg"]}

        with (
            patch("hft_platform.reports.pipeline.build_report", side_effect=side_effect),
            patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await _push_report(ctx, "day")

        # Only 1 message sent (TXFD6), NOSYMBOL skipped
        assert ctx.bot.send_message.call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot_scheduler.py::TestMultiSymbolPush -v`
Expected: FAIL — current `_push_report` only calls `build_report` once

- [ ] **Step 3: Rewrite `_push_report` in `scheduler.py`**

Replace the existing `_push_report`, `_get_symbol`, and `_get_owner_chat_id` functions:

```python
def _get_owner_chat_id() -> str:
    return os.environ.get("HFT_TELEGRAM_CHAT_ID", "")


async def _push_report(context: Any, session: str) -> None:
    """Push reports for all configured symbols."""
    import hft_platform.bot.app as bot_app
    from hft_platform.bot.app import get_report_symbols
    from hft_platform.reports.pipeline import build_report, resolve_trading_date

    chat_id = _get_owner_chat_id()
    if not chat_id:
        _log.error("bot.push_no_chat_id")
        return

    date = resolve_trading_date(session)
    symbols = get_report_symbols()
    _log.info("bot.push_start", session=session, date=date, symbols=symbols)

    for symbol in symbols:
        try:
            rendered = build_report(session, date, symbol)
            bot_app.last_ch_ok = datetime.now(_TZ)
        except Exception as exc:
            _log.error("bot.push_error", session=session, symbol=symbol, exc=str(exc), exc_info=True)
            continue

        if rendered is None:
            _log.info("bot.push_no_data", session=session, date=date, symbol=symbol)
            continue

        for msg in rendered["paid"]:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
            await asyncio.sleep(1.5)

    now = datetime.now(_TZ)
    if session == "day":
        bot_app.last_day_report = now
    else:
        bot_app.last_night_report = now

    _log.info("bot.push_complete", session=session, date=date, symbols=len(symbols))
```

- [ ] **Step 4: Run all scheduler tests**

Run: `uv run pytest tests/unit/test_bot_scheduler.py -v`
Expected: ALL PASSED

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/bot/scheduler.py tests/unit/test_bot_scheduler.py
git commit -m "feat(bot): multi-symbol push loop in scheduler"
```

---

### Task 5: Add equity large-trade thresholds to `collector.py`

**Files:**
- Modify: `src/hft_platform/reports/collector.py`

- [ ] **Step 1: Verify current thresholds**

Run: `uv run pytest tests/ -k "test_" --co -q 2>/dev/null | grep -i "large_trade\|collector" | head -20`

Check if there are existing tests for collector thresholds. If not, this is a trivial config change — add and commit.

- [ ] **Step 2: Add equity thresholds**

In `src/hft_platform/reports/collector.py`, update the `_LARGE_TRADE_THRESHOLDS` dict (line 42):

```python
_LARGE_TRADE_THRESHOLDS: dict[str, int] = {
    "TXFD6": 10,
    "TMFD6": 30,
    "MXFD6": 30,
    "2330": 100,
}
```

- [ ] **Step 3: Run existing tests**

Run: `uv run pytest tests/unit/ -k "report" -v --timeout=30`
Expected: ALL PASSED (no behavior change for existing symbols)

- [ ] **Step 4: Commit**

```bash
git add src/hft_platform/reports/collector.py
git commit -m "feat(reports): add 2330 equity large-trade threshold"
```

---

### Task 6: Update `/start` welcome message

**Files:**
- Modify: `src/hft_platform/bot/handlers.py`
- Test: `tests/unit/test_bot_handlers.py`

- [ ] **Step 1: Update the welcome text**

In `src/hft_platform/bot/handlers.py`, update the `cmd_start` function text:

```python
@owner_only
async def cmd_start(update: Any, context: Any) -> None:
    """Handle /start command."""
    text = (
        "HFT 市場分析 Bot\n\n"
        "可用指令：\n"
        "/report [symbol] [day|night] — 完整分析報告\n"
        "/levels [symbol] — 支撐壓力位\n"
        "/flow [symbol] — 流向摘要\n"
        "/status — Bot 運行狀態\n\n"
        "symbol 可省略，預設使用第一個設定商品"
    )
    await update.message.reply_text(text)
```

- [ ] **Step 2: Run start handler test**

Run: `uv run pytest tests/unit/test_bot_handlers.py::TestStartHandler -v`
Expected: PASSED (test checks for `/report`, `/levels`, `/flow`, `/status` substrings — all present)

- [ ] **Step 3: Commit**

```bash
git add src/hft_platform/bot/handlers.py
git commit -m "feat(bot): update /start welcome text with symbol arg docs"
```

---

### Task 7: Docker Compose — add `hft-bot` service

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add bot extra to Dockerfile**

In `Dockerfile`, find the line that generates `requirements.txt` from `pyproject.toml` dependencies (around line 33-42). The generated file only includes `[project].dependencies`, not optional extras. Add the bot extra after the main pip install (around line 102):

```dockerfile
RUN pip install --no-cache-dir --timeout 600 --retries 10 -r requirements.txt
# Install Rust extension wheel (fast-path helpers)
RUN pip install --no-cache-dir /tmp/wheels/*.whl
# Install bot optional extra (python-telegram-bot)
RUN pip install --no-cache-dir "python-telegram-bot[job-queue]>=21.0"
```

- [ ] **Step 2: Add `hft-bot` service to `docker-compose.yml`**

Add after the last service definition (before `volumes:` or `networks:` sections):

```yaml
  hft-bot:
    <<: *hft-common
    container_name: hft-bot
    command: ["sh", "-lc", "python -m hft_platform.bot"]
    environment:
      - HFT_TELEGRAM_BOT_TOKEN=${HFT_TELEGRAM_BOT_TOKEN:?HFT_TELEGRAM_BOT_TOKEN must be set}
      - HFT_TELEGRAM_CHAT_ID=${HFT_TELEGRAM_CHAT_ID:?HFT_TELEGRAM_CHAT_ID must be set}
      - HFT_REPORT_SYMBOLS=${HFT_REPORT_SYMBOLS:-TXFD6}
      - HFT_CLICKHOUSE_HOST=clickhouse
      - CLICKHOUSE_PASSWORD=${CLICKHOUSE_PASSWORD:?CLICKHOUSE_PASSWORD must be set in .env}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
      - TZ=Asia/Taipei
    depends_on:
      clickhouse:
        condition: service_healthy
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 256M
          cpus: "0.5"
    healthcheck:
      test: ["CMD-SHELL", "true"]
      interval: 60s
      timeout: 5s
      retries: 3
```

Note: The `<<: *hft-common` inherits the build context and common env. Bot-specific env vars override the common ones. The healthcheck is a simple pass-through since the bot has no HTTP endpoint.

- [ ] **Step 3: Validate compose file**

Run: `docker compose config --quiet 2>&1 | head -5`
Expected: No errors (or warnings about unset env vars, which is OK for validation)

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml Dockerfile
git commit -m "feat(ops): add hft-bot service to Docker Compose"
```

---

### Task 8: Run full test suite and lint

**Files:** None (verification only)

- [ ] **Step 1: Run all bot tests**

Run: `uv run pytest tests/unit/test_bot_handlers.py tests/unit/test_bot_scheduler.py -v`
Expected: ALL PASSED

- [ ] **Step 2: Run lint**

Run: `uv run ruff check src/hft_platform/bot/ tests/unit/test_bot_handlers.py tests/unit/test_bot_scheduler.py`
Expected: No errors

- [ ] **Step 3: Run type check (if applicable)**

Run: `uv run mypy src/hft_platform/bot/ --ignore-missing-imports`
Expected: No errors (or pre-existing ones only)

- [ ] **Step 4: Final commit if lint/type fixes needed**

```bash
git add -u
git commit -m "fix(bot): lint and type fixes"
```
