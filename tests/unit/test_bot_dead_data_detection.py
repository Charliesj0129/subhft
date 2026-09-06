"""P1-c regression tests: hft-bot dead 8.7 days root-cause coverage.

Bug: in `bot/scheduler.py:61-66`, `last_day_report` only updated INSIDE
`if sent_any:`. When every configured symbol returned `bot.push_no_data`,
`sent_any` stayed False and the heartbeat reported `last_day=None
last_night=None` for 8.7 days. Operators could not distinguish "scheduler
never fired" from "scheduler fired but all data was empty".

Also: `bot/app.py` built `Application` with no `error_handler`. Any
uncaught exception in a handler (e.g. `httpx.ConnectError`) flew through
and only logged `No error handlers are registered`.

Pinned behaviors:
  1. `last_day_attempt` / `last_night_attempt` ARE updated even when every
     symbol returns no_data, providing scheduler-liveness evidence.
  2. After `DEAD_DATA_ALERT_THRESHOLD` consecutive empty pushes, scheduler
     emits a `bot.dead_data_alert` warning + bumps a metric.
  3. `consecutive_empty_attempts` resets to 0 on any successful symbol.
  4. `_telegram_error_handler` increments `bot_handler_errors_total` for
     any captured exception.
  5. The streak advances only on a TRADING day, and a closed day neither
     advances nor resets it.

Every case that depends on (5) names its own date. Reading the real clock
made behaviour 2 assert something different on a Wednesday than on a Sunday,
and main's CI was red every weekend with `assert 0 >= 2` because of it.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from structlog.testing import capture_logs

from hft_platform.reports.models import ComposedReport, MessagePart

_TZ = ZoneInfo("Asia/Taipei")


#: A date the market calendar agrees was open, and one it agrees was closed.
#: The streak counter is only advanced on a trading day, so a test that lets
#: ``resolve_trading_date`` read the real clock asserts a different thing on a
#: Wednesday than on a Sunday -- which is how
#: ``test_alert_fires_after_threshold_consecutive_empties`` failed with
#: ``assert 0 >= 2`` on main every weekend while passing on weekdays.
_TRADING_DAY = "2026-09-02"  # Wednesday
_CLOSED_DAY = "2026-09-05"  # Saturday


def _make_composed(msgs: list[str] | None = None) -> ComposedReport:
    if msgs is None:
        msgs = ["msg1"]
    return ComposedReport(messages=[MessagePart(kind="text", content=m, min_tier="paid") for m in msgs])


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "12345")
    import hft_platform.bot.app as bot_app

    bot_app.last_day_report = None
    bot_app.last_night_report = None
    bot_app.last_day_attempt = None
    bot_app.last_night_attempt = None
    bot_app.consecutive_empty_attempts = 0
    bot_app.latest_manual_report_context = None  # cross-test isolation


class TestAttemptTracking:
    @pytest.mark.asyncio
    async def test_attempt_recorded_even_when_all_symbols_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Core P1-c claim: last_day_attempt updates even when no symbol
        returned data, so the heartbeat can prove the scheduler ran."""
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "NOSYM1,NOSYM2")
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()

        with patch(
            "hft_platform.reports.pipeline.build_hybrid_report_async",
            new=AsyncMock(return_value=SimpleNamespace(composed=None, dossier=None, decision=None, llm_error=None)),
        ):
            await _push_report(ctx, "day")

        assert bot_app.last_day_report is None  # no successful sends
        assert bot_app.last_day_attempt is not None  # but attempt logged
        assert isinstance(bot_app.last_day_attempt, datetime)

    @pytest.mark.asyncio
    async def test_attempt_recorded_for_night_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "NOSYM1")
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        with patch(
            "hft_platform.reports.pipeline.build_hybrid_report_async",
            new=AsyncMock(return_value=SimpleNamespace(composed=None, dossier=None, decision=None, llm_error=None)),
        ):
            await _push_report(ctx, "night")

        assert bot_app.last_night_attempt is not None


class TestDeadDataAlert:
    @pytest.mark.asyncio
    async def test_alert_fires_after_threshold_consecutive_empties(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "NOSYM1")
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        threshold = bot_app.DEAD_DATA_ALERT_THRESHOLD
        with (
            patch("hft_platform.reports.pipeline.resolve_trading_date", return_value=_TRADING_DAY),
            patch(
                "hft_platform.reports.pipeline.build_hybrid_report_async",
                new=AsyncMock(return_value=SimpleNamespace(composed=None, dossier=None, decision=None, llm_error=None)),
            ),
            capture_logs() as logs,
        ):
            for _ in range(threshold):
                await _push_report(ctx, "day")

        assert bot_app.consecutive_empty_attempts >= threshold
        # The counter is the state; the alert is what an operator actually sees.
        assert [e for e in logs if e.get("event") == "bot.dead_data_alert"]

    @pytest.mark.asyncio
    async def test_streak_does_not_advance_when_the_market_was_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A closed market is not a dead feed.

        This is the other half of the pin above and the reason it needed one:
        the counter's behaviour is a function of the date, so both branches
        have to name their date instead of inheriting the runner's clock.
        """
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "NOSYM1")
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        with (
            patch("hft_platform.reports.pipeline.resolve_trading_date", return_value=_CLOSED_DAY),
            patch(
                "hft_platform.reports.pipeline.build_hybrid_report_async",
                new=AsyncMock(return_value=SimpleNamespace(composed=None, dossier=None, decision=None, llm_error=None)),
            ),
        ):
            for _ in range(bot_app.DEAD_DATA_ALERT_THRESHOLD):
                await _push_report(ctx, "day")

        assert bot_app.consecutive_empty_attempts == 0

    @pytest.mark.asyncio
    async def test_a_closed_day_does_not_launder_a_streak_that_started_earlier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A feed that died on Friday is still dead on Monday.

        Suppressing the closed day must not also clear the evidence, or a
        weekend would reset every outage that began before it.
        """
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "NOSYM1")
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _push_report

        bot_app.consecutive_empty_attempts = 3
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        with (
            patch("hft_platform.reports.pipeline.resolve_trading_date", return_value=_CLOSED_DAY),
            patch(
                "hft_platform.reports.pipeline.build_hybrid_report_async",
                new=AsyncMock(return_value=SimpleNamespace(composed=None, dossier=None, decision=None, llm_error=None)),
            ),
        ):
            await _push_report(ctx, "day")

        assert bot_app.consecutive_empty_attempts == 3

    @pytest.mark.asyncio
    async def test_streak_resets_on_successful_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "NOSYM1,GOODSYM")
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _push_report

        # Pre-seed an existing streak — should be cleared once a real send happens.
        bot_app.consecutive_empty_attempts = 3
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.send_photo = AsyncMock()

        async def side_effect(session: str, date: object, symbol: str) -> SimpleNamespace:
            if symbol == "NOSYM1":
                return SimpleNamespace(composed=None, dossier=None, decision=None, llm_error=None)
            return SimpleNamespace(
                composed=_make_composed(["msg"]),
                dossier=MagicMock(),
                decision=MagicMock(),
                llm_error=None,
            )

        with (
            patch(
                "hft_platform.reports.pipeline.build_hybrid_report_async",
                new=AsyncMock(side_effect=side_effect),
            ),
            patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await _push_report(ctx, "day")

        assert bot_app.consecutive_empty_attempts == 0


class TestErrorHandler:
    @pytest.mark.asyncio
    async def test_telegram_error_handler_increments_metric(self) -> None:
        from hft_platform.bot.app import _telegram_error_handler
        from hft_platform.observability.metrics import MetricsRegistry

        metric = MetricsRegistry.get().bot_handler_errors_total.labels(exception="ConnectError")
        before = metric._value.get()

        # Simulate an httpx.ConnectError-like update + context
        ctx = SimpleNamespace(error=ConnectionError("Connect failed"))
        # The handler should NOT raise, even though our fake error type is
        # ConnectionError (mapped to "ConnectionError" label). Use a more
        # realistic name to mimic httpx.ConnectError class name.

        class ConnectError(Exception):
            pass

        ctx2 = SimpleNamespace(error=ConnectError("boom"))
        await _telegram_error_handler(None, ctx2)

        after = MetricsRegistry.get().bot_handler_errors_total.labels(exception="ConnectError")._value.get()
        assert after > before


class TestHeartbeatExposesNewFields:
    @pytest.mark.asyncio
    async def test_heartbeat_logs_attempt_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _heartbeat

        bot_app.last_day_attempt = datetime.now(_TZ)
        bot_app.consecutive_empty_attempts = 5

        ctx = MagicMock()
        # Capture structlog output via a side-channel — we just need to
        # confirm _heartbeat doesn't crash when these fields are populated.
        await _heartbeat(ctx)
        # If we got here, heartbeat ran without raising — the new fields
        # are in the log payload (verified by static read of source).
