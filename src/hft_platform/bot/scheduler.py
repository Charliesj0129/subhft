"""Scheduled push jobs for the Telegram Bot."""

from __future__ import annotations

import asyncio
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
        # P1-c: every symbol returned no_data. Increment the streak counter.
        bot_app.consecutive_empty_attempts += 1
        threshold = bot_app.DEAD_DATA_ALERT_THRESHOLD
        if bot_app.consecutive_empty_attempts >= threshold:
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

    _log.info("bot.push_complete", session=session, date=date, symbols=len(symbols))


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


def schedule_jobs(job_queue: Any) -> None:
    """Register scheduled jobs on the JobQueue."""
    # Day report: 13:50 CST, Mon-Fri
    job_queue.run_daily(
        _push_day,
        time=time(hour=13, minute=50, tzinfo=_TZ),
        days=(0, 1, 2, 3, 4),
        name="push_day_report",
    )

    # Night report: 05:05 CST, Mon-Sat
    job_queue.run_daily(
        _push_night,
        time=time(hour=5, minute=5, tzinfo=_TZ),
        days=(0, 1, 2, 3, 4, 5),
        name="push_night_report",
    )

    # Heartbeat: every 5 minutes
    job_queue.run_repeating(
        _heartbeat,
        interval=300,
        name="heartbeat",
    )

    _log.info("bot.jobs_scheduled")
