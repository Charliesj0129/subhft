# Telegram Bot Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wrap the existing report pipeline into an interactive Telegram Bot with scheduled push and on-demand commands (`/report`, `/levels`, `/flow`, `/status`).

**Architecture:** Bot is a trigger+transport layer only — all analysis reuses `reports/` modules. New `bot/` package (4 files) handles command routing and scheduling via `python-telegram-bot`. Runs as independent Docker container. Two prerequisite refactors: extract `build_report()` from pipeline and add `collect_core()` to collector.

**Tech Stack:** Python 3.12, `python-telegram-bot[job-queue]>=21.0`, existing ClickHouse + `reports/` modules.

**Spec:** `docs/superpowers/specs/2026-03-29-telegram-bot-service-design.md`

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| Modify | `src/hft_platform/reports/collector.py` | Add `collect_core()` — Q1-Q4 only |
| Modify | `src/hft_platform/reports/pipeline.py` | Extract `build_report()` returning rendered dict |
| Modify | `pyproject.toml` | Add `bot` optional dependency group |
| Modify | `docker-compose.yml` | Add `hft-bot` service |
| Create | `src/hft_platform/bot/__init__.py` | Package init |
| Create | `src/hft_platform/bot/__main__.py` | Entry point |
| Create | `src/hft_platform/bot/app.py` | Application setup, polling, owner-only middleware |
| Create | `src/hft_platform/bot/handlers.py` | Command handlers |
| Create | `src/hft_platform/bot/scheduler.py` | Scheduled push jobs |
| Create | `tests/unit/test_bot_handlers.py` | Handler unit tests |
| Create | `tests/unit/test_bot_scheduler.py` | Scheduler unit tests |
| Create | `tests/unit/test_report_pipeline_build.py` | Tests for `build_report()` |
| Create | `tests/unit/test_collector_core.py` | Tests for `collect_core()` |

---

### Task 1: Add `collect_core()` to DataCollector

**Files:**
- Modify: `src/hft_platform/reports/collector.py:140-207`
- Create: `tests/unit/test_collector_core.py`

**Context:** The current `DataCollector.collect()` runs all 6 CH queries. `/levels` and `/flow` commands only need Q1-Q4 (OHLCV, bars, flow, large trades). We extract the Q1-Q4 portion into `collect_core()` and have `collect()` call it, then add Q5/Q6.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_collector_core.py
"""Tests for DataCollector.collect_core() lightweight query path."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.reports.collector import DataCollector, _day_filter
from hft_platform.reports.models import SessionData


class TestCollectCore:
    def test_returns_session_data_with_empty_spread_and_depth(self) -> None:
        """collect_core() skips Q5/Q6 and returns empty spread_dist and depth_imbalance."""
        collector = DataCollector.__new__(DataCollector)

        # Mock _execute to return canned data for Q1-Q4
        call_count = 0

        def fake_execute(sql: str) -> list[tuple]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:  # Q1 OHLCV
                return [(200000000, 210000000, 190000000, 205000000, 1000, 50)]
            if call_count == 2:  # Q2 5m bars
                return []
            if call_count == 3:  # Q3 flow
                return []
            if call_count == 4:  # Q4 large trades
                return []
            raise AssertionError(f"Unexpected query #{call_count}")

        collector._execute = fake_execute

        time_filter = _day_filter("2026-03-28")
        result = collector.collect_core("TXFD6", time_filter)

        assert isinstance(result, SessionData)
        assert result.spread_dist == {}
        assert result.depth_imbalance == []
        assert result.volume == 1000
        assert call_count == 4  # Only 4 queries, not 6

    def test_collect_delegates_to_collect_core(self) -> None:
        """collect() calls collect_core() internally, then adds Q5/Q6."""
        collector = DataCollector.__new__(DataCollector)

        query_sqls: list[str] = []

        def fake_execute(sql: str) -> list[tuple]:
            query_sqls.append(sql)
            if "argMin" in sql:  # Q1
                return [(200000000, 210000000, 190000000, 205000000, 1000, 50)]
            return []

        collector._execute = fake_execute

        result = collector.collect("day", "2026-03-28")

        # Should have 6 queries total (4 from core + 2 from spread/depth)
        assert len(query_sqls) == 6
        assert result.spread_dist == {}  # Empty because fake returns []
        assert result.depth_imbalance == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_collector_core.py -v`
Expected: FAIL — `DataCollector` has no `collect_core` method.

- [ ] **Step 3: Extract `collect_core()` from `collect()`**

In `src/hft_platform/reports/collector.py`, refactor the `DataCollector` class. Add `collect_core()` method that runs Q1-Q4 and returns `SessionData` with empty spread/depth. Modify `collect()` to call `collect_core()` then add Q5/Q6.

```python
    # Inside class DataCollector:

    def collect_core(
        self,
        symbol: str,
        time_filter: str,
        *,
        session: str = "",
        date: str = "",
    ) -> SessionData:
        """Collect core data (Q1-Q4) for *symbol* with the given *time_filter*.

        Skips the heavier spread (Q5) and depth (Q6) queries.
        Returns SessionData with ``spread_dist={}`` and ``depth_imbalance=[]``.
        """
        ohlcv = self._query_ohlcv(symbol, time_filter)
        bars = self._query_5m_bars(symbol, time_filter)
        flow = self._query_flow(symbol, time_filter)
        large = self._query_large_trades(symbol, time_filter)

        return SessionData(
            session=session,
            symbol=symbol,
            date=date,
            open=ScaledPrice(ohlcv["open"]),
            high=ScaledPrice(ohlcv["high"]),
            low=ScaledPrice(ohlcv["low"]),
            close=ScaledPrice(ohlcv["close"]),
            volume=ohlcv["volume"],
            tick_count=ohlcv["tick_count"],
            bars_5m=bars,
            flow_5m=flow,
            large_trades=large,
            spread_dist={},
            depth_imbalance=[],
        )

    def collect(
        self,
        session: str,
        date: str,
        symbol: str = "TXFD6",
    ) -> SessionData:
        """Collect all data for *symbol* on *date* for the given *session*."""
        time_filter = _day_filter(date) if session == "day" else _night_filter(date)

        sd = self.collect_core(symbol, time_filter, session=session, date=date)

        # Q5 and Q6 — heavy queries, gracefully degrade on OOM
        try:
            spread = self._query_spread_dist(symbol, time_filter)
        except Exception:  # noqa: BLE001
            log.warning("Q5 spread query failed (likely OOM), skipping", symbol=symbol)
            spread = {}
        try:
            depth = self._query_depth_imbalance(symbol, time_filter)
        except Exception:  # noqa: BLE001
            log.warning("Q6 depth query failed (likely OOM), skipping", symbol=symbol)
            depth = []

        # Return new SessionData with spread/depth filled in
        return SessionData(
            session=sd.session,
            symbol=sd.symbol,
            date=sd.date,
            open=sd.open,
            high=sd.high,
            low=sd.low,
            close=sd.close,
            volume=sd.volume,
            tick_count=sd.tick_count,
            bars_5m=sd.bars_5m,
            flow_5m=sd.flow_5m,
            large_trades=sd.large_trades,
            spread_dist=spread,
            depth_imbalance=depth,
        )
```

Also add `collect_core` to the `__all__` list in the module — no, it's a method not a module function. Just ensure the class is already exported. Check: `DataCollector` is already in `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_collector_core.py -v`
Expected: PASS

- [ ] **Step 5: Run existing collector tests to verify no regression**

Run: `uv run pytest tests/unit/test_report_distributor.py tests/unit/test_report_rules_sr.py -v`
Expected: PASS (existing tests unchanged)

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/reports/collector.py tests/unit/test_collector_core.py
git commit -m "refactor(reports): extract collect_core() for lightweight Q1-Q4 queries"
```

---

### Task 2: Extract `build_report()` from pipeline

**Files:**
- Modify: `src/hft_platform/reports/pipeline.py:72-154`
- Create: `tests/unit/test_report_pipeline_build.py`

**Context:** The current `run_pipeline()` sends messages directly via Distributor and returns `None`. The bot needs rendered messages returned. We extract a `build_report()` function that runs stages 1-4 and returns the rendered dict, or `None` if no data.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_report_pipeline_build.py
"""Tests for build_report() extracted pipeline function."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.reports.models import SessionData
from hft_platform.reports.pipeline import build_report


class TestBuildReport:
    def test_returns_rendered_dict_on_success(self) -> None:
        """build_report() returns dict with 'free' and 'paid' keys."""
        fake_sd = MagicMock(spec=SessionData)
        fake_sd.tick_count = 100
        fake_sd.bars_5m = []

        with (
            patch("hft_platform.reports.pipeline.DataCollector") as MockCollector,
            patch("hft_platform.reports.pipeline.SignalEngine") as MockSignal,
            patch("hft_platform.reports.pipeline.ScenarioBuilder") as MockScenario,
            patch("hft_platform.reports.pipeline.ReportRenderer") as MockRenderer,
        ):
            MockCollector.return_value.collect.return_value = fake_sd
            MockSignal.return_value.analyze.return_value = MagicMock()
            MockScenario.return_value.build.return_value = MagicMock()
            MockRenderer.return_value.render.side_effect = lambda report, tier: [f"{tier} msg"]

            result = build_report("day", "2026-03-28")

        assert result is not None
        assert "free" in result
        assert "paid" in result
        assert result["paid"] == ["paid msg"]
        assert result["free"] == ["free msg"]

    def test_returns_none_when_no_data(self) -> None:
        """build_report() returns None when tick_count == 0."""
        fake_sd = MagicMock(spec=SessionData)
        fake_sd.tick_count = 0
        fake_sd.bars_5m = []

        with patch("hft_platform.reports.pipeline.DataCollector") as MockCollector:
            MockCollector.return_value.collect.return_value = fake_sd

            result = build_report("day", "2026-03-28")

        assert result is None

    def test_default_symbol_is_txfd6(self) -> None:
        """build_report() uses TXFD6 as default symbol."""
        fake_sd = MagicMock(spec=SessionData)
        fake_sd.tick_count = 0
        fake_sd.bars_5m = []

        with patch("hft_platform.reports.pipeline.DataCollector") as MockCollector:
            MockCollector.return_value.collect.return_value = fake_sd

            build_report("day", "2026-03-28")

            MockCollector.return_value.collect.assert_called_once_with("day", "2026-03-28", "TXFD6")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_report_pipeline_build.py -v`
Expected: FAIL — `build_report` does not exist.

- [ ] **Step 3: Implement `build_report()` and refactor `run_pipeline()`**

In `src/hft_platform/reports/pipeline.py`, add `build_report()` and simplify `run_pipeline()`:

```python
# Add to __all__
__all__ = ["resolve_trading_date", "build_report", "run_pipeline", "main"]


def build_report(
    session: str,
    date: str,
    symbol: str = "TXFD6",
) -> dict[str, list[str]] | None:
    """Run stages 1-4 of the report pipeline and return rendered messages.

    Returns:
        A dict mapping tier ("free", "paid") to list of message strings,
        or None if there is no data for the given session/date.
    """
    from hft_platform.reports.collector import DataCollector
    from hft_platform.reports.renderer import ReportRenderer
    from hft_platform.reports.scenarios import ScenarioBuilder
    from hft_platform.reports.signals import SignalEngine

    _log.info("build_report_start", session=session, date=date, symbol=symbol)

    # Stage 1: collect session data
    collector = DataCollector()
    session_data = collector.collect(session, date, symbol)
    _log.info("stage1_complete", ticks=session_data.tick_count, bars=len(session_data.bars_5m))

    if session_data.tick_count == 0:
        _log.warning("build_report_empty_session", session=session, date=date)
        return None

    # Stage 2: derive signals
    engine = SignalEngine()
    signal_report = engine.analyze(session_data)
    _log.info("stage2_complete", bias=signal_report.bias, confidence=signal_report.bias_confidence)

    # Stage 3: build scenarios
    builder = ScenarioBuilder()
    scenario_report = builder.build(signal_report)
    _log.info("stage3_complete", direction=scenario_report.direction, scenarios=len(scenario_report.scenarios))

    # Stage 4: render messages
    renderer = ReportRenderer()
    rendered = {
        "free": renderer.render(scenario_report, tier="free"),
        "paid": renderer.render(scenario_report, tier="paid"),
    }
    _log.info("stage4_complete", free_msgs=len(rendered["free"]), paid_msgs=len(rendered["paid"]))

    return rendered


async def run_pipeline(
    session: str,
    date: str,
    *,
    dry_run: bool = False,
    debug: bool = False,
) -> None:
    """Execute the full report pipeline for the given session and date."""
    rendered = build_report(session, date)

    if rendered is None:
        return

    if debug:
        for tier, msgs in rendered.items():
            print(f"\n{'=' * 40} {tier.upper()} {'=' * 40}")
            for i, m in enumerate(msgs, 1):
                print(f"\n--- Message {i}/{len(msgs)} ({len(m)} chars) ---")
                print(m)

    if dry_run:
        _log.info("report_pipeline_dry_run_complete")
        return

    # Stage 5: distribute
    from hft_platform.reports.distributor import Distributor, ReportSender, load_channels

    channels = load_channels()
    sender = ReportSender()
    distributor = Distributor(sender=sender, channels=channels)
    try:
        await distributor.send(rendered)
    finally:
        await sender.close()

    _log.info("report_pipeline_complete", session=session, date=date)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_report_pipeline_build.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/pipeline.py tests/unit/test_report_pipeline_build.py
git commit -m "refactor(reports): extract build_report() returning rendered messages"
```

---

### Task 3: Add `python-telegram-bot` dependency

**Files:**
- Modify: `pyproject.toml:61-64`

**Context:** Add `python-telegram-bot[job-queue]` as an optional dependency group `bot`. This keeps the main platform slim — only the bot container installs it.

- [ ] **Step 1: Add the dependency group**

In `pyproject.toml`, add after the existing `monitor` line (line 64):

```toml
[project.optional-dependencies]
shioaji-broker = ["shioaji[speed]"]  # Shioaji (SinoPac) broker SDK
fubon = ["fubon-neo>=2.2.7"]  # Fubon broker SDK (not on PyPI; install from Fubon's index)
monitor = ["rich>=13.0"]  # Signal monitor TUI (rich.live + rich.table)
bot = ["python-telegram-bot[job-queue]>=21.0"]  # Telegram Bot interactive service
```

- [ ] **Step 2: Verify resolution**

Run: `uv pip install -e ".[bot]" --dry-run 2>&1 | head -20`
Expected: Shows `python-telegram-bot` and `APScheduler` in resolution output, no errors.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add python-telegram-bot[job-queue] as optional 'bot' dependency"
```

---

### Task 4: Bot package — `__init__.py` and `__main__.py`

**Files:**
- Create: `src/hft_platform/bot/__init__.py`
- Create: `src/hft_platform/bot/__main__.py`

**Context:** Minimal entry point. `__main__.py` allows `python -m hft_platform.bot` to start the bot.

- [ ] **Step 1: Create the package init**

```python
# src/hft_platform/bot/__init__.py
"""Telegram Bot interactive service for the HFT Market Analysis Report."""
```

- [ ] **Step 2: Create the entry point**

```python
# src/hft_platform/bot/__main__.py
"""Entry point: ``python -m hft_platform.bot``."""

from __future__ import annotations

from hft_platform.bot.app import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Commit**

```bash
git add src/hft_platform/bot/__init__.py src/hft_platform/bot/__main__.py
git commit -m "feat(bot): add bot package skeleton with __main__ entry point"
```

---

### Task 5: Bot app — Application setup, owner-only middleware, polling

**Files:**
- Create: `src/hft_platform/bot/app.py`
- Create: `tests/unit/test_bot_handlers.py` (partial — access control tests)

**Context:** `app.py` creates the `python-telegram-bot` Application, registers all command handlers, sets up the owner-only access control decorator, and starts polling. The access control decorator checks `update.effective_chat.id` against `HFT_TELEGRAM_CHAT_ID`.

- [ ] **Step 1: Write access control test**

```python
# tests/unit/test_bot_handlers.py
"""Unit tests for bot command handlers and access control."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

OWNER_CHAT_ID = "12345"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", OWNER_CHAT_ID)


def _make_update(chat_id: int, text: str = "/start") -> MagicMock:
    """Create a mock telegram Update with the given chat_id."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context() -> MagicMock:
    """Create a mock CallbackContext."""
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.args = []
    return ctx


class TestAccessControl:
    @pytest.mark.asyncio
    async def test_owner_allowed(self) -> None:
        from hft_platform.bot.app import owner_only

        @owner_only
        async def dummy_handler(update: MagicMock, context: MagicMock) -> None:
            update.message.reply_text("ok")

        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await dummy_handler(update, ctx)

        update.message.reply_text.assert_any_call("ok")

    @pytest.mark.asyncio
    async def test_non_owner_rejected(self) -> None:
        from hft_platform.bot.app import owner_only

        called = False

        @owner_only
        async def dummy_handler(update: MagicMock, context: MagicMock) -> None:
            nonlocal called
            called = True

        update = _make_update(chat_id=99999)
        ctx = _make_context()
        await dummy_handler(update, ctx)

        assert not called
        update.message.reply_text.assert_called_once_with("未授權")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bot_handlers.py::TestAccessControl -v`
Expected: FAIL — `hft_platform.bot.app` does not exist.

- [ ] **Step 3: Implement `app.py`**

```python
# src/hft_platform/bot/app.py
"""BotApp: initialise python-telegram-bot Application, register handlers, start polling."""

from __future__ import annotations

import functools
import os
from datetime import datetime
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo

import structlog

_log = structlog.get_logger(__name__)

_TZ = ZoneInfo("Asia/Taipei")

# ---------------------------------------------------------------------------
# Shared state (module-level, updated by handlers/scheduler)
# ---------------------------------------------------------------------------

start_time: datetime = datetime.now(_TZ)
last_day_report: datetime | None = None
last_night_report: datetime | None = None
last_ch_ok: datetime | None = None

# ---------------------------------------------------------------------------
# Owner-only access control
# ---------------------------------------------------------------------------

_OWNER_CHAT_ID: int = 0


def _get_owner_chat_id() -> int:
    global _OWNER_CHAT_ID  # noqa: PLW0603
    if _OWNER_CHAT_ID == 0:
        raw = os.environ.get("HFT_TELEGRAM_CHAT_ID", "")
        _OWNER_CHAT_ID = int(raw) if raw else 0
    return _OWNER_CHAT_ID


HandlerFunc = Callable[..., Coroutine[Any, Any, None]]


def owner_only(func: HandlerFunc) -> HandlerFunc:
    """Decorator that restricts handler to the configured owner chat_id."""

    @functools.wraps(func)
    async def wrapper(update: Any, context: Any) -> None:
        chat_id = update.effective_chat.id
        if chat_id != _get_owner_chat_id():
            _log.warning("bot.unauthorized", chat_id=chat_id)
            await update.message.reply_text("未授權")
            return
        await func(update, context)

    return wrapper


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app() -> Any:
    """Build and return a configured ``telegram.ext.Application``."""
    from telegram.ext import Application, CommandHandler

    from hft_platform.bot.handlers import cmd_flow, cmd_levels, cmd_report, cmd_start, cmd_status
    from hft_platform.bot.scheduler import schedule_jobs

    token = os.environ.get("HFT_TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("HFT_TELEGRAM_BOT_TOKEN is required")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("levels", cmd_levels))
    app.add_handler(CommandHandler("flow", cmd_flow))
    app.add_handler(CommandHandler("status", cmd_status))

    schedule_jobs(app.job_queue)

    _log.info("bot.app_created")
    return app


def main() -> None:
    """Entry point: create app and start polling."""
    global start_time  # noqa: PLW0603
    start_time = datetime.now(_TZ)

    app = create_app()
    _log.info("bot.started")
    app.run_polling(drop_pending_updates=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bot_handlers.py::TestAccessControl -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/bot/app.py tests/unit/test_bot_handlers.py
git commit -m "feat(bot): add app.py with owner-only access control and Application factory"
```

---

### Task 6: Bot handlers — `/start`, `/report`, `/levels`, `/flow`, `/status`

**Files:**
- Create: `src/hft_platform/bot/handlers.py`
- Modify: `tests/unit/test_bot_handlers.py` (add handler tests)

**Context:** Each handler is decorated with `@owner_only`. `/report` calls `build_report()`. `/levels` and `/flow` call `collect_core()`. `/status` reads in-memory state from `app.py`.

- [ ] **Step 1: Write handler tests**

Append to `tests/unit/test_bot_handlers.py`:

```python
class TestStartHandler:
    @pytest.mark.asyncio
    async def test_start_replies_with_menu(self) -> None:
        from hft_platform.bot.handlers import cmd_start

        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_start(update, ctx)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "/report" in reply_text
        assert "/levels" in reply_text
        assert "/flow" in reply_text
        assert "/status" in reply_text


class TestReportHandler:
    @pytest.mark.asyncio
    async def test_report_sends_paid_messages(self) -> None:
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report day")
        ctx = _make_context()
        ctx.args = ["day"]

        with patch("hft_platform.bot.handlers.build_report") as mock_build:
            mock_build.return_value = {"paid": ["msg1", "msg2"], "free": ["msg1"]}
            with patch("hft_platform.bot.handlers.asyncio") as mock_asyncio:
                mock_asyncio.sleep = AsyncMock()
                await cmd_report(update, ctx)

        # Should send "產生報告中..." then each paid message
        calls = update.message.reply_text.call_args_list
        assert "產生報告中" in calls[0][0][0]

        # The paid messages are sent via context.bot.send_message
        send_calls = ctx.bot.send_message.call_args_list
        assert len(send_calls) == 2
        assert send_calls[0].kwargs["text"] == "msg1"
        assert send_calls[1].kwargs["text"] == "msg2"

    @pytest.mark.asyncio
    async def test_report_no_data_replies_message(self) -> None:
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report day")
        ctx = _make_context()
        ctx.args = ["day"]

        with patch("hft_platform.bot.handlers.build_report") as mock_build:
            mock_build.return_value = None
            await cmd_report(update, ctx)

        calls = update.message.reply_text.call_args_list
        assert any("無交易資料" in str(c) for c in calls)


class TestStatusHandler:
    @pytest.mark.asyncio
    async def test_status_includes_uptime(self) -> None:
        from hft_platform.bot.handlers import cmd_status

        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_status(update, ctx)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "運行時間" in reply_text


class TestLevelsHandler:
    @pytest.mark.asyncio
    async def test_levels_returns_sr_text(self) -> None:
        from hft_platform.bot.handlers import cmd_levels

        update = _make_update(chat_id=12345)
        ctx = _make_context()

        fake_sd = MagicMock()
        fake_sd.tick_count = 100
        fake_sd.session = "day"
        fake_sd.date = "2026-03-28"
        fake_sd.symbol = "TXFD6"

        fake_signal = MagicMock()
        fake_signal.supports = []
        fake_signal.resistances = []

        with (
            patch("hft_platform.bot.handlers._collect_core_for_latest") as mock_collect,
            patch("hft_platform.bot.handlers.SignalEngine") as MockSignal,
        ):
            mock_collect.return_value = fake_sd
            MockSignal.return_value.analyze.return_value = fake_signal
            await cmd_levels(update, ctx)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "支撐壓力位" in reply_text


class TestFlowHandler:
    @pytest.mark.asyncio
    async def test_flow_returns_summary(self) -> None:
        from hft_platform.bot.handlers import cmd_flow

        update = _make_update(chat_id=12345)
        ctx = _make_context()

        fake_sd = MagicMock()
        fake_sd.tick_count = 100
        fake_sd.session = "day"
        fake_sd.date = "2026-03-28"
        fake_sd.symbol = "TXFD6"
        fake_sd.volume = 50000
        fake_sd.flow_5m = []
        fake_sd.large_trades = []

        with patch("hft_platform.bot.handlers._collect_core_for_latest") as mock_collect:
            mock_collect.return_value = fake_sd
            await cmd_flow(update, ctx)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "流向摘要" in reply_text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot_handlers.py -v -k "not AccessControl"`
Expected: FAIL — `hft_platform.bot.handlers` does not exist.

- [ ] **Step 3: Implement `handlers.py`**

```python
# src/hft_platform/bot/handlers.py
"""Command handlers for the Telegram Bot."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from hft_platform.bot.app import last_ch_ok, owner_only, start_time

_log = structlog.get_logger(__name__)
_TZ = ZoneInfo("Asia/Taipei")

PLATFORM_SCALE = 10_000


def _get_symbol() -> str:
    return os.environ.get("HFT_BOT_SYMBOL", "TXFD6")


def _collect_core_for_latest() -> Any:
    """Run collect_core() for the most recent session."""
    from hft_platform.reports.collector import DataCollector, _day_filter, _night_filter
    from hft_platform.reports.pipeline import resolve_trading_date

    now = datetime.now(_TZ)
    # If between 05:00-13:45, latest is night session from yesterday/today.
    # If between 13:45-15:00, latest is day session.
    # If after 15:00, latest is (ongoing) night session.
    # Simplification: try day first, if no data try night.
    symbol = _get_symbol()

    for session in ("day", "night"):
        date = resolve_trading_date(session, now=now)
        time_filter = _day_filter(date) if session == "day" else _night_filter(date)
        collector = DataCollector()
        sd = collector.collect_core(symbol, time_filter, session=session, date=date)
        if sd.tick_count > 0:
            return sd

    # Return last attempt even if empty
    return sd


@owner_only
async def cmd_start(update: Any, context: Any) -> None:
    """Handle /start command."""
    text = (
        "HFT 市場分析 Bot\n\n"
        "可用指令：\n"
        "/report [day|night] — 取得完整分析報告\n"
        "/levels — 當前支撐壓力位\n"
        "/flow — 最新流向摘要\n"
        "/status — Bot 運行狀態"
    )
    await update.message.reply_text(text)


@owner_only
async def cmd_report(update: Any, context: Any) -> None:
    """Handle /report [day|night] command."""
    import hft_platform.bot.app as bot_app
    from hft_platform.reports.pipeline import build_report, resolve_trading_date

    args = context.args or []
    if args and args[0] in ("day", "night"):
        session = args[0]
    else:
        # Auto-detect: if 07:00-14:59 → day, else night
        now = datetime.now(_TZ)
        session = "day" if 7 <= now.hour < 15 else "night"

    date = resolve_trading_date(session)

    await update.message.reply_text(f"產生報告中... ({session} {date})")

    try:
        rendered = build_report(session, date, _get_symbol())
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


@owner_only
async def cmd_levels(update: Any, context: Any) -> None:
    """Handle /levels command."""
    import hft_platform.bot.app as bot_app
    from hft_platform.reports.signals import SignalEngine

    try:
        sd = _collect_core_for_latest()
        bot_app.last_ch_ok = datetime.now(_TZ)
    except Exception as exc:
        _log.error("bot.levels_error", exc=str(exc), exc_info=True)
        await update.message.reply_text("資料庫暫時不可用，請稍後再試")
        return

    if sd.tick_count == 0:
        await update.message.reply_text("該時段無交易資料")
        return

    engine = SignalEngine()
    signal = engine.analyze(sd)

    session_label = "日盤" if sd.session == "day" else "夜盤"
    lines = [f"支撐壓力位 ({sd.symbol} {session_label} {sd.date})\n"]

    if signal.resistances:
        lines.append("壓力：")
        for i, r in enumerate(signal.resistances, 1):
            stars = "★" * max(1, int(r.strength * 3))
            lines.append(f"  R{i}: {r.price // PLATFORM_SCALE:,} {stars} {r.reason}")

    if signal.supports:
        lines.append("\n支撐：")
        for i, s in enumerate(signal.supports, 1):
            stars = "★" * max(1, int(s.strength * 3))
            lines.append(f"  S{i}: {s.price // PLATFORM_SCALE:,} {stars} {s.reason}")

    if not signal.resistances and not signal.supports:
        lines.append("（未偵測到顯著支撐壓力位）")

    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_flow(update: Any, context: Any) -> None:
    """Handle /flow command."""
    import hft_platform.bot.app as bot_app

    try:
        sd = _collect_core_for_latest()
        bot_app.last_ch_ok = datetime.now(_TZ)
    except Exception as exc:
        _log.error("bot.flow_error", exc=str(exc), exc_info=True)
        await update.message.reply_text("資料庫暫時不可用，請稍後再試")
        return

    if sd.tick_count == 0:
        await update.message.reply_text("該時段無交易資料")
        return

    session_label = "日盤" if sd.session == "day" else "夜盤"

    # Compute session-level U/D
    total_up = sum(f.uptick_vol for f in sd.flow_5m)
    total_dn = sum(f.downtick_vol for f in sd.flow_5m)
    ud_ratio = total_up / total_dn if total_dn > 0 else (float(total_up) if total_up > 0 else 1.0)

    if ud_ratio >= 1.1:
        bias_label = "偏多"
    elif ud_ratio <= 0.9:
        bias_label = "偏空"
    else:
        bias_label = "中性"

    buy_trades = sum(1 for t in sd.large_trades if t.direction == "buy")
    sell_trades = sum(1 for t in sd.large_trades if t.direction == "sell")

    lines = [
        f"流向摘要 ({sd.symbol} {session_label} {sd.date})\n",
        f"U/D Ratio: {ud_ratio:.2f} ({bias_label})",
        f"成交量: {sd.volume:,}",
        f"大單: 買 {buy_trades} 筆 / 賣 {sell_trades} 筆",
    ]

    # Last 5 flow bars
    recent = sd.flow_5m[-5:] if sd.flow_5m else []
    if recent:
        lines.append("\n最近 5 根 K棒流向：")
        for bar in recent:
            arrow = "▲" if bar.ud_ratio >= 1.0 else "▼"
            lines.append(f"{bar.ts[-8:-3]} {arrow} {bar.ud_ratio:.2f}")

    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_status(update: Any, context: Any) -> None:
    """Handle /status command."""
    import hft_platform.bot.app as bot_app

    now = datetime.now(_TZ)
    uptime = now - bot_app.start_time
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes = remainder // 60

    lines = [
        "Bot 狀態\n",
        f"運行時間: {hours}h {minutes}m",
    ]

    if bot_app.last_day_report:
        lines.append(f"上次日盤報告: {bot_app.last_day_report.strftime('%Y-%m-%d %H:%M')} CST")
    else:
        lines.append("上次日盤報告: —")

    if bot_app.last_night_report:
        lines.append(f"上次夜盤報告: {bot_app.last_night_report.strftime('%Y-%m-%d %H:%M')} CST")
    else:
        lines.append("上次夜盤報告: —")

    if bot_app.last_ch_ok:
        ch_ago = now - bot_app.last_ch_ok
        ch_mins = int(ch_ago.total_seconds()) // 60
        lines.append(f"ClickHouse: 最後成功 {ch_mins} 分鐘前")
    else:
        lines.append("ClickHouse: 尚未連線")

    await update.message.reply_text("\n".join(lines))
```

- [ ] **Step 4: Run all handler tests**

Run: `uv run pytest tests/unit/test_bot_handlers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/bot/handlers.py tests/unit/test_bot_handlers.py
git commit -m "feat(bot): add command handlers — /start, /report, /levels, /flow, /status"
```

---

### Task 7: Bot scheduler — scheduled daily push

**Files:**
- Create: `src/hft_platform/bot/scheduler.py`
- Create: `tests/unit/test_bot_scheduler.py`

**Context:** Uses `python-telegram-bot`'s `JobQueue.run_daily()` to schedule day (13:50 Mon-Fri) and night (05:05 Mon-Sat) report push. Each job calls `build_report()`, sends paid messages to the owner, and updates the last-report timestamp.

- [ ] **Step 1: Write scheduler tests**

```python
# tests/unit/test_bot_scheduler.py
"""Unit tests for bot scheduled push jobs."""

from __future__ import annotations

from datetime import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "12345")


class TestScheduleJobs:
    def test_registers_two_daily_jobs(self) -> None:
        from hft_platform.bot.scheduler import schedule_jobs

        job_queue = MagicMock()
        schedule_jobs(job_queue)

        assert job_queue.run_daily.call_count == 2

    def test_day_report_schedule(self) -> None:
        from hft_platform.bot.scheduler import schedule_jobs

        job_queue = MagicMock()
        schedule_jobs(job_queue)

        # First call is day report
        call_kwargs = job_queue.run_daily.call_args_list[0]
        assert call_kwargs.kwargs["time"].hour == 13
        assert call_kwargs.kwargs["time"].minute == 50
        # Mon-Fri = days (0,1,2,3,4)
        assert set(call_kwargs.kwargs["days"]) == {0, 1, 2, 3, 4}

    def test_night_report_schedule(self) -> None:
        from hft_platform.bot.scheduler import schedule_jobs

        job_queue = MagicMock()
        schedule_jobs(job_queue)

        # Second call is night report
        call_kwargs = job_queue.run_daily.call_args_list[1]
        assert call_kwargs.kwargs["time"].hour == 5
        assert call_kwargs.kwargs["time"].minute == 5
        # Mon-Sat = days (0,1,2,3,4,5)
        assert set(call_kwargs.kwargs["days"]) == {0, 1, 2, 3, 4, 5}


class TestPushJob:
    @pytest.mark.asyncio
    async def test_push_sends_messages_on_success(self) -> None:
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()

        with patch("hft_platform.bot.scheduler.build_report") as mock_build:
            mock_build.return_value = {"paid": ["msg1", "msg2"], "free": ["fmsg"]}
            with patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio:
                mock_asyncio.sleep = AsyncMock()
                await _push_report(ctx, "day")

        assert ctx.bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_push_no_data_does_nothing(self) -> None:
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()

        with patch("hft_platform.bot.scheduler.build_report") as mock_build:
            mock_build.return_value = None
            await _push_report(ctx, "day")

        ctx.bot.send_message.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_bot_scheduler.py -v`
Expected: FAIL — `hft_platform.bot.scheduler` does not exist.

- [ ] **Step 3: Implement `scheduler.py`**

```python
# src/hft_platform/bot/scheduler.py
"""Scheduled push jobs for the Telegram Bot."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import structlog

_log = structlog.get_logger(__name__)
_TZ = ZoneInfo("Asia/Taipei")


def _get_owner_chat_id() -> str:
    return os.environ.get("HFT_TELEGRAM_CHAT_ID", "")


def _get_symbol() -> str:
    return os.environ.get("HFT_BOT_SYMBOL", "TXFD6")


async def _push_report(context: Any, session: str) -> None:
    """Shared logic for scheduled report push."""
    import hft_platform.bot.app as bot_app
    from hft_platform.reports.pipeline import build_report, resolve_trading_date

    chat_id = _get_owner_chat_id()
    if not chat_id:
        _log.error("bot.push_no_chat_id")
        return

    date = resolve_trading_date(session)
    _log.info("bot.push_start", session=session, date=date)

    try:
        rendered = build_report(session, date, _get_symbol())
        bot_app.last_ch_ok = datetime.now(_TZ)
    except Exception as exc:
        _log.error("bot.push_error", session=session, exc=str(exc), exc_info=True)
        return

    if rendered is None:
        _log.info("bot.push_no_data", session=session, date=date)
        return

    for msg in rendered["paid"]:
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        await asyncio.sleep(1.5)

    now = datetime.now(_TZ)
    if session == "day":
        bot_app.last_day_report = now
    else:
        bot_app.last_night_report = now

    _log.info("bot.push_complete", session=session, date=date, messages=len(rendered["paid"]))


async def _push_day(context: Any) -> None:
    await _push_report(context, "day")


async def _push_night(context: Any) -> None:
    await _push_report(context, "night")


async def _heartbeat(context: Any) -> None:
    """Log heartbeat with uptime and last report timestamps."""
    import hft_platform.bot.app as bot_app

    now = datetime.now(_TZ)
    uptime_s = int((now - bot_app.start_time).total_seconds())
    _log.info(
        "bot.heartbeat",
        uptime_s=uptime_s,
        last_day=str(bot_app.last_day_report),
        last_night=str(bot_app.last_night_report),
    )


def schedule_jobs(job_queue: Any) -> None:
    """Register scheduled jobs on the JobQueue."""
    # Day report: 13:50 CST, Mon-Fri
    job_queue.run_daily(
        _push_day,
        time=time(hour=13, minute=50, tzinfo=_TZ),
        days=(0, 1, 2, 3, 4),  # Mon-Fri
        name="push_day_report",
    )

    # Night report: 05:05 CST, Mon-Sat
    job_queue.run_daily(
        _push_night,
        time=time(hour=5, minute=5, tzinfo=_TZ),
        days=(0, 1, 2, 3, 4, 5),  # Mon-Sat
        name="push_night_report",
    )

    # Heartbeat: every 5 minutes
    job_queue.run_repeating(
        _heartbeat,
        interval=300,
        name="heartbeat",
    )

    _log.info("bot.jobs_scheduled")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_bot_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/bot/scheduler.py tests/unit/test_bot_scheduler.py
git commit -m "feat(bot): add scheduled push jobs — day 13:50, night 05:05, heartbeat"
```

---

### Task 8: Docker integration — add `hft-bot` service

**Files:**
- Modify: `docker-compose.yml` (append new service after existing services)

**Context:** Add `hft-bot` as an independent service. Does NOT use `*hft-common` anchor — declares its own minimal environment. No healthcheck (spec decision). Depends on ClickHouse healthy.

- [ ] **Step 1: Add `hft-bot` service to docker-compose.yml**

Append at the end of the `services:` block (before the `volumes:` block at the bottom of the file):

```yaml
  hft-bot:
    build: .
    container_name: hft-bot
    command: ["python", "-m", "hft_platform.bot"]
    environment:
      - HFT_TELEGRAM_BOT_TOKEN=${HFT_TELEGRAM_BOT_TOKEN}
      - HFT_TELEGRAM_CHAT_ID=${HFT_TELEGRAM_CHAT_ID}
      - HFT_CLICKHOUSE_HOST=clickhouse
      - CLICKHOUSE_USER=${CLICKHOUSE_USER:-default}
      - CLICKHOUSE_PASSWORD=${CLICKHOUSE_PASSWORD:-}
      - HFT_BOT_SYMBOL=${HFT_BOT_SYMBOL:-TXFD6}
      - TZ=Asia/Taipei
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
      - PYTHONDONTWRITEBYTECODE=1
    volumes:
      - ./src:/app/src
    depends_on:
      clickhouse:
        condition: service_healthy
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '0.25'
          memory: 512M
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "3"
```

- [ ] **Step 2: Validate compose syntax**

Run: `cd /home/charlie/hft_platform && docker compose config --quiet 2>&1 | head -5`
Expected: No errors (or only warnings about unset variables which is fine for validation).

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(bot): add hft-bot service to docker-compose"
```

---

### Task 9: Full integration test — `/report` end-to-end with mocked CH

**Files:**
- Modify: `tests/unit/test_bot_handlers.py` (add integration test)

**Context:** An end-to-end test that exercises the full `/report` flow with mocked ClickHouse: handler → build_report → DataCollector (mocked) → SignalEngine → ScenarioBuilder → Renderer → message sending.

- [ ] **Step 1: Write integration test**

Append to `tests/unit/test_bot_handlers.py`:

```python
class TestReportIntegration:
    """Integration test: /report with real pipeline stages but mocked CH."""

    @pytest.mark.asyncio
    async def test_full_report_flow(self) -> None:
        from hft_platform.bot.handlers import cmd_report
        from hft_platform.reports.models import (
            Bar5m,
            FlowBar,
            LargeTrade,
            SessionData,
        )

        SCALE = 10_000

        fake_sd = SessionData(
            session="day",
            symbol="TXFD6",
            date="2026-03-28",
            open=20000 * SCALE,
            high=20500 * SCALE,
            low=19500 * SCALE,
            close=20200 * SCALE,
            volume=50000,
            tick_count=1000,
            bars_5m=[
                Bar5m(ts="2026-03-28 09:00:00", open=20000 * SCALE, high=20200 * SCALE,
                      low=19900 * SCALE, close=20100 * SCALE, volume=5000, ticks=100),
                Bar5m(ts="2026-03-28 09:05:00", open=20100 * SCALE, high=20500 * SCALE,
                      low=20000 * SCALE, close=20300 * SCALE, volume=6000, ticks=120),
            ],
            flow_5m=[
                FlowBar(ts="2026-03-28 09:00:00", ticks=100, total_vol=5000,
                         uptick_vol=3000, downtick_vol=2000, flat_vol=0, ud_ratio=1.5, net_flow=1000),
                FlowBar(ts="2026-03-28 09:05:00", ticks=120, total_vol=6000,
                         uptick_vol=2000, downtick_vol=4000, flat_vol=0, ud_ratio=0.5, net_flow=-2000),
            ],
            large_trades=[
                LargeTrade(ts="2026-03-28 09:02:00", price=20100 * SCALE, volume=30, direction="buy"),
            ],
            spread_dist={1: 500, 2: 300},
            depth_imbalance=[],
        )

        update = _make_update(chat_id=12345, text="/report day")
        ctx = _make_context()
        ctx.args = ["day"]

        with patch("hft_platform.reports.pipeline.DataCollector") as MockCollector:
            MockCollector.return_value.collect.return_value = fake_sd
            with patch("hft_platform.bot.handlers.asyncio") as mock_asyncio:
                mock_asyncio.sleep = AsyncMock()
                await cmd_report(update, ctx)

        # Should have sent placeholder + multiple paid messages
        assert update.message.reply_text.call_count >= 1
        send_calls = ctx.bot.send_message.call_args_list
        assert len(send_calls) >= 3  # At least summary + flow + levels
        # Verify messages are strings with content
        for call in send_calls:
            msg_text = call.kwargs["text"]
            assert isinstance(msg_text, str)
            assert len(msg_text) > 50  # Not trivially empty
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/unit/test_bot_handlers.py::TestReportIntegration -v`
Expected: PASS

- [ ] **Step 3: Run all bot tests together**

Run: `uv run pytest tests/unit/test_bot_handlers.py tests/unit/test_bot_scheduler.py tests/unit/test_collector_core.py tests/unit/test_report_pipeline_build.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_bot_handlers.py
git commit -m "test(bot): add full /report integration test with mocked ClickHouse"
```

---

### Task 10: Lint, typecheck, and final verification

**Files:** None new — validation only.

- [ ] **Step 1: Run ruff lint**

Run: `uv run ruff check src/hft_platform/bot/ tests/unit/test_bot_handlers.py tests/unit/test_bot_scheduler.py tests/unit/test_collector_core.py tests/unit/test_report_pipeline_build.py`
Expected: No errors. Fix any issues found.

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest tests/unit/ -v --timeout=30 -x -q 2>&1 | tail -20`
Expected: All tests pass, no regressions.

- [ ] **Step 3: Verify bot starts locally (smoke test)**

Run: `cd /home/charlie/hft_platform && HFT_TELEGRAM_BOT_TOKEN=fake HFT_TELEGRAM_CHAT_ID=12345 timeout 3 python -m hft_platform.bot 2>&1 || true`
Expected: Logs `bot.app_created` and `bot.started`, then exits after 3s timeout (or fails on network since token is fake — that's fine, the import chain works).

- [ ] **Step 4: Final commit if any lint fixes were needed**

```bash
git add -u
git commit -m "chore: fix lint issues in bot module"
```
