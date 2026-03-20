"""Session awareness: trading hours detection and next-open countdown."""

from __future__ import annotations

import datetime as dt

from structlog import get_logger

from hft_platform.core.market_calendar import get_calendar

logger = get_logger("monitor.session")

# Module-level cached timezone (Phase 2d)
try:
    from zoneinfo import ZoneInfo

    _TZ_TAIPEI: dt.tzinfo = ZoneInfo("Asia/Taipei")
except Exception as exc:
    logger.debug("operation_fallback", error=str(exc))
    _TZ_TAIPEI = dt.timezone(dt.timedelta(hours=8))


def get_session_info(
    product_type: str,
    now: dt.datetime | None = None,
) -> tuple[bool, str, str]:
    """Check trading session status for a product type.

    Returns:
        (is_active, session_label, session_display)
        - is_active: True if in trading hours
        - session_label: "" | "[PRE]" | "[CLOSED]"
        - session_display: e.g. "Day Session", "Night Session", "Closed"
    """
    cal = get_calendar()
    if now is None:
        now = dt.datetime.now(_TZ_TAIPEI)

    is_trading = cal.is_trading_hours(now, product_type=product_type)

    if is_trading:
        current_min = now.hour * 60 + now.minute
        if product_type in ("future", "option"):
            if current_min >= 15 * 60 or current_min < 5 * 60:
                return True, "", "Night Session"
            return True, "", "Day Session"
        return True, "", "Day Session"

    # Check pre-market
    current_min = now.hour * 60 + now.minute
    if product_type in ("future", "option"):
        if 8 * 60 + 30 <= current_min < 8 * 60 + 45:
            return False, "[PRE]", "Pre-market"
    elif product_type == "stock":
        if 8 * 60 + 30 <= current_min < 9 * 60:
            return False, "[PRE]", "Pre-market"

    return False, "[CLOSED]", "Closed"


def get_session_start(
    product_type: str,
    now: dt.datetime | None = None,
) -> dt.datetime | None:
    """Return the current active session start, or None if outside trading hours."""
    if now is None:
        now = dt.datetime.now(_TZ_TAIPEI)

    cal = get_calendar()
    if not cal.is_trading_hours(now, product_type=product_type):
        return None

    if product_type in ("future", "option"):
        current_min = now.hour * 60 + now.minute
        if current_min >= 15 * 60:
            return now.replace(hour=15, minute=0, second=0, microsecond=0)
        if current_min < 5 * 60:
            prev_day = now - dt.timedelta(days=1)
            return prev_day.replace(hour=15, minute=0, second=0, microsecond=0)
        return now.replace(hour=8, minute=45, second=0, microsecond=0)

    return now.replace(hour=9, minute=0, second=0, microsecond=0)


def format_next_open(product_type: str, now: dt.datetime | None = None) -> str:
    """Format countdown to next trading session open."""
    cal = get_calendar()
    tz = _TZ_TAIPEI

    if now is None:
        now = dt.datetime.now(tz)

    if product_type in ("future", "option"):
        current_min = now.hour * 60 + now.minute
        if cal.is_trading_day(now.date()) and current_min < 8 * 60 + 45:
            open_time = now.replace(hour=8, minute=45, second=0, microsecond=0)
            return _format_countdown("Day", open_time, open_time - now)
        if cal.is_trading_day(now.date()) and current_min < 15 * 60:
            open_time = now.replace(hour=15, minute=0, second=0, microsecond=0)
            return _format_countdown("Night", open_time, open_time - now)
    elif cal.is_trading_day(now.date()) and (now.hour * 60 + now.minute) < 9 * 60:
        open_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
        return _format_countdown("Day", open_time, open_time - now)

    next_day = cal.next_trading_day(now.date())
    if next_day is None:
        return "Next: unknown"

    open_time = (
        dt.datetime.combine(next_day, dt.time(8, 45), tzinfo=tz)
        if product_type in ("future", "option")
        else dt.datetime.combine(next_day, dt.time(9, 0), tzinfo=tz)
    )

    delta = open_time - now
    day_name = open_time.strftime("%a")
    time_str = open_time.strftime("%H:%M")
    return f"Next: {day_name} {time_str} TST (in {_format_timedelta(delta)})"


def _format_countdown(session: str, open_time: dt.datetime, delta: dt.timedelta) -> str:
    time_str = open_time.strftime("%H:%M")
    return f"Next: {session} {time_str} TST (in {_format_timedelta(delta)})"


def _format_timedelta(td: dt.timedelta) -> str:
    total_s = int(td.total_seconds())
    if total_s < 0:
        return "now"
    hours, remainder = divmod(total_s, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"
