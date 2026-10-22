"""Last-trading-day facts for TAIFEX monthly futures, read on the exchange clock.

A monthly future stops trading at 13:30 Taipei on its delivery date and is then
cash-settled. A position still open at that moment is closed by the exchange at
the final settlement price, outside every control the platform has: on THESHOW
on 2026-10-21 R47 carried TMFJ6 +1 into settlement, the strategy rebound to
TMFK6 and opened another lot next to it, and the settled lot stayed in the
position store until a restart dropped it unbooked. The cutoff here is the point
after which the platform stops adding to such a position and starts closing it.

The delivery date is derived from the code (third Wednesday,
``taifex_monthly_delivery_date``), the same rule ``startup_recon`` uses. TAIFEX
moves delivery to the next business day when that Wednesday is a holiday and the
derivation does not; on such a month the guard fires one business day early,
which is the safe direction.
"""

from __future__ import annotations

import datetime as dt

from hft_platform.contracts.ref import FutureRef, parse_display
from hft_platform.core.market_calendar import taifex_monthly_delivery_date

_NS_PER_DAY = 86_400 * 1_000_000_000
_NS_PER_MINUTE = 60 * 1_000_000_000
# Taipei is UTC+8 with no daylight saving.
_TAIPEI_OFFSET_NS = 8 * 3600 * 1_000_000_000
_EPOCH = dt.date(1970, 1, 1)

# Default cutoff: 13:00 Taipei, 30 minutes before the last trading session ends.
DEFAULT_EXPIRY_CUTOFF_MINUTE = 13 * 60


def taipei_date_and_minute(now_ns: int) -> tuple[dt.date, int]:
    """Taipei calendar date and minute of day for an epoch-ns timestamp (integer arithmetic only)."""
    local_ns = now_ns + _TAIPEI_OFFSET_NS
    days, rem = divmod(local_ns, _NS_PER_DAY)
    return _EPOCH + dt.timedelta(days=days), rem // _NS_PER_MINUTE


def parse_cutoff_minute(value: object, default: int = DEFAULT_EXPIRY_CUTOFF_MINUTE) -> int:
    """Read an ``"HH:MM"`` cutoff from config. Anything unreadable keeps ``default``."""
    if not isinstance(value, str):
        return default
    hh, sep, mm = value.strip().partition(":")
    if not sep or not hh.isdigit() or not mm.isdigit():
        return default
    hour, minute = int(hh), int(mm)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return default
    return hour * 60 + minute


def monthly_delivery_date(symbol: str, base_year: int) -> dt.date | None:
    """Delivery date of a monthly TAIFEX future code, or None for anything else (stocks, options, families)."""
    try:
        ref = parse_display(symbol, base_year=base_year)
    except ValueError:
        return None
    if not isinstance(ref, FutureRef):
        return None
    return taifex_monthly_delivery_date(ref.expiry.year, ref.expiry.month)


# Returned by ``expiry_window_ns`` for anything with no monthly delivery date.
NEVER_NS = 2**63 - 1


def expiry_window_ns(symbol: str, now_ns: int, cutoff_minute: int) -> tuple[int, int]:
    """Epoch-ns ``[start, end)`` of ``symbol``'s expiry window, or ``(NEVER_NS, NEVER_NS)``.

    The window runs from ``cutoff_minute`` on the delivery date to the end of
    that Taipei day. Only the delivery date itself: a contract that has already
    settled has no market to close it in (startup recovery drops it), and a
    code whose year was misread must not block trading in a live contract for
    good. Computed once per symbol by callers on the order path, which then
    compare integers instead of building dates per intent.
    """
    today, _ = taipei_date_and_minute(now_ns)
    delivery = monthly_delivery_date(symbol, base_year=today.year)
    if delivery is None:
        return NEVER_NS, NEVER_NS
    day_start = (delivery - _EPOCH).days * _NS_PER_DAY - _TAIPEI_OFFSET_NS
    return day_start + cutoff_minute * _NS_PER_MINUTE, day_start + _NS_PER_DAY


def expiry_cutoff_passed(symbol: str, now_ns: int, cutoff_minute: int) -> bool:
    """True on ``symbol``'s delivery date at or after ``cutoff_minute`` (Taipei)."""
    start, end = expiry_window_ns(symbol, now_ns, cutoff_minute)
    return start <= now_ns < end
