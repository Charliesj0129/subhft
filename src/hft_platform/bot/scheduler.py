"""Scheduled push jobs for the Telegram Bot."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import os
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import structlog

_log = structlog.get_logger(__name__)
_TZ = ZoneInfo("Asia/Taipei")


def _get_owner_chat_id() -> str:
    return os.environ.get("HFT_TELEGRAM_CHAT_ID", "")


def _is_trading_day(date: str) -> bool:
    """Was the market open on ``date``? Fails **open**, on purpose.

    Any doubt -- an unparseable date, a missing calendar, an import failure --
    resolves to True, so an empty session still counts toward the dead-data
    streak. Suppressing a real feed outage is the expensive mistake here; one
    spurious alert on a holiday is not.
    """
    try:
        from hft_platform.core.market_calendar import get_calendar

        return get_calendar().is_trading_day(dt.date.fromisoformat(date))
    except Exception as exc:  # noqa: BLE001 -- calendar is advisory, never a gate
        _log.debug("bot.trading_day_check_failed", date=date, error=str(exc))
        return True


async def _push_report(context: Any, session: str) -> None:
    """Push reports for all configured symbols.

    P1-c (2026-04-27): track ATTEMPTS separately from successes so the
    heartbeat can distinguish "scheduler never fired" from "scheduler fired
    but every symbol returned no_data". After ``DEAD_DATA_ALERT_THRESHOLD``
    consecutive empty attempts, emit a ``bot.dead_data_alert`` warning so
    operators see the issue instead of silently believing the bot is healthy.
    """
    import hft_platform.bot.app as bot_app
    from hft_platform.bot.app import get_report_symbols
    from hft_platform.reports.pipeline import build_hybrid_report_async, resolve_trading_date

    chat_id = _get_owner_chat_id()
    if not chat_id:
        _log.error("bot.push_no_chat_id")
        return

    date = resolve_trading_date(session)
    symbols = get_report_symbols()
    _log.info("bot.push_start", session=session, date=date, symbols=symbols)

    # P1-c: record the attempt itself unconditionally — even if every symbol
    # returns no_data, the scheduler DID run and we have evidence of liveness.
    now_attempt = datetime.now(_TZ)
    if session == "day":
        bot_app.last_day_attempt = now_attempt
    else:
        bot_app.last_night_attempt = now_attempt

    sent_any = False
    for symbol in symbols:
        try:
            result = await build_hybrid_report_async(session, date, symbol)
            bot_app.last_ch_ok = datetime.now(_TZ)
        except Exception as exc:
            _log.error("bot.push_error", session=session, symbol=symbol, exc=str(exc), exc_info=True)
            continue

        if result.composed is None:
            _log.info("bot.push_no_data", session=session, date=date, symbol=symbol)
            continue
        if result.llm_error:
            _log.warning("bot.push_llm_fallback", session=session, date=date, symbol=symbol, llm_error=result.llm_error)

        for i, part in enumerate(result.composed.messages):
            if part.kind == "text":
                await context.bot.send_message(chat_id=chat_id, text=part.content, parse_mode="HTML")
            elif part.kind == "image" and part.image is not None:
                await context.bot.send_photo(chat_id=chat_id, photo=io.BytesIO(part.image), caption=part.caption)
            if i < len(result.composed.messages) - 1:
                await asyncio.sleep(1.5)
        sent_any = True

    if sent_any:
        now = datetime.now(_TZ)
        if session == "day":
            bot_app.last_day_report = now
        else:
            bot_app.last_night_report = now
        # Reset empty-attempt counter on any successful symbol.
        bot_app.consecutive_empty_attempts = 0
    else:
        _record_empty_attempt(session, date, symbols)

    _log.info("bot.push_complete", session=session, date=date, symbols=len(symbols))


def _record_empty_attempt(session: str, date: str, symbols: list[str]) -> None:
    """Every symbol returned no_data. Decide whether that is worth paging about.

    P1-c (2026-04-27): track the streak so operators see a dead feed instead of
    silently believing the bot is healthy.
    """
    import hft_platform.bot.app as bot_app

    if not _is_trading_day(date):
        # A market that was closed is not a dead feed. The weekday sets in
        # schedule_jobs keep the scheduler off weekends, but they cannot know
        # about a holiday, and the streak counter reads "no rows" as evidence of
        # an ingestion fault either way. Counting a closed session pages an
        # operator with "likely an upstream feed/CK ingestion issue" for a market
        # that simply was not trading -- which is exactly how THESHOW sent two of
        # those every single weekend for at least a month.
        #
        # Deliberately does NOT reset the streak: a feed that died on Friday is
        # still dead on Monday, and a holiday in between must not launder it.
        _log.info("bot.push_skipped_market_closed", session=session, date=date, symbols=symbols)
        return

    bot_app.consecutive_empty_attempts += 1
    threshold = bot_app.DEAD_DATA_ALERT_THRESHOLD
    if bot_app.consecutive_empty_attempts < threshold:
        return

    _log.warning(
        "bot.dead_data_alert",
        session=session,
        date=date,
        consecutive_empty_attempts=bot_app.consecutive_empty_attempts,
        threshold=threshold,
        symbols=symbols,
        hint=(
            "Scheduled push fired but all symbols returned no_data — "
            "likely an upstream feed/CK ingestion issue, not a bot bug."
        ),
    )
    try:
        from hft_platform.observability.metrics import MetricsRegistry

        MetricsRegistry.get().bot_dead_data_alerts_total.labels(session=session).inc()
    except Exception:  # noqa: BLE001 — observability is best-effort
        pass


async def _push_day(context: Any) -> None:
    await _push_report(context, "day")


async def _push_night(context: Any) -> None:
    await _push_report(context, "night")


async def _heartbeat(context: Any) -> None:
    """Log heartbeat with uptime and last report timestamps.

    P1-c (2026-04-27): include attempt timestamps and empty-streak counter so
    a heartbeat that says `last_day=None last_night=None` can still show
    `last_day_attempt=...` to prove the scheduler is alive.
    """
    import hft_platform.bot.app as bot_app

    now = datetime.now(_TZ)
    uptime_s = int((now - bot_app.start_time).total_seconds())
    _log.info(
        "bot.heartbeat",
        uptime_s=uptime_s,
        last_day=str(bot_app.last_day_report),
        last_night=str(bot_app.last_night_report),
        last_day_attempt=str(bot_app.last_day_attempt),
        last_night_attempt=str(bot_app.last_night_attempt),
        consecutive_empty_attempts=bot_app.consecutive_empty_attempts,
    )


# python-telegram-bot's ``run_daily(days=...)`` maps 0-6 to **Sunday-Saturday**.
# It mapped 0-6 to Monday-Sunday before v20.0 and the library's own changelog
# calls the change out; this module was written against the old numbering and
# never updated, while the host runs PTB 22.8. The comments therefore described
# a schedule the code did not implement:
#
#   day   (0,1,2,3,4)   read as Mon-Fri  ->  actually Sun,Mon,Tue,Wed,Thu
#   night (0,1,2,3,4,5) read as Mon-Sat  ->  actually Sun,Mon,Tue,Wed,Thu,Fri
#
# Two reports a week were silently lost and two junk ones sent instead, which is
# exactly what THESHOW's logs show: no day report on Friday 2026-08-21 (the
# heartbeat still read ``last_day=2026-08-20`` on the Saturday), no 05:05 run at
# all on Saturday 2026-08-22 -- the run that carries the *Friday night session* --
# and two runs over Sunday that could only ever find an empty market and page
# ``bot.dead_data_alert``.
_SUN, _MON, _TUE, _WED, _THU, _FRI, _SAT = range(7)


def schedule_jobs(job_queue: Any) -> None:
    """Register scheduled jobs on the JobQueue."""
    # Day report: 13:50 CST, covering the day session that closed at 13:45.
    job_queue.run_daily(
        _push_day,
        time=time(hour=13, minute=50, tzinfo=_TZ),
        days=(_MON, _TUE, _WED, _THU, _FRI),
        name="push_day_report",
    )

    # Night report: 05:05 CST, covering the night session that just closed at
    # 05:00. A TAIFEX night session opens 15:00 on a trading day and closes
    # 05:00 the *next* calendar day, so the sessions are Mon->Tue through
    # Fri->Sat and the reports land Tuesday through Saturday. Monday 05:05 is
    # deliberately absent: there is no Sunday-night session for it to report.
    job_queue.run_daily(
        _push_night,
        time=time(hour=5, minute=5, tzinfo=_TZ),
        days=(_TUE, _WED, _THU, _FRI, _SAT),
        name="push_night_report",
    )

    # Heartbeat: every 5 minutes
    job_queue.run_repeating(
        _heartbeat,
        interval=300,
        name="heartbeat",
    )

    _log.info("bot.jobs_scheduled")
