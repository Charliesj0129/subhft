"""Row types, exact price scaling, time conversion and calendar helpers for the historical ingester.

Timestamp labels, as far as can be told without a live login (all UNVERIFIED):
- Shioaji ticks and kbars: ``ts`` is LOCAL Taipei epoch ns (scripts/backfill_historical_ticks.py); stored as UTC.
  The kbar label is the bar END: verified live 2026-11-21 on TMF, 2283 of 2283 one-minute volumes equal the tick
  sums of the minute before the label, 8 of 2279 of the same minute.
- Fubon candles: ``date`` is ISO-8601 with +08:00; the documented sample starts at 08:45, so the label is the bar
  START (not yet checked live).
``bar_ts`` is always the bar START in UTC: Shioaji labels are shifted back one bar width (SHIOAJI_KBAR_NS).
"""

from __future__ import annotations

import datetime as dt
from collections import namedtuple
from decimal import Decimal
from typing import Any

PRICE_SCALE = 1_000_000  # research ClickHouse scale (x1,000,000)
NS_PER_S = 1_000_000_000
TAIPEI = dt.timezone(dt.timedelta(hours=8))  # no DST in Taiwan
TAIPEI_OFFSET_NS = 8 * 3600 * NS_PER_S
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
DAY_OPEN_MIN = 8 * 60 + 45
DAY_CLOSE_MIN = 14 * 60  # 13:45 close plus margin
INTRADAY_MAX_TICK_DAYS = 8

DONE, EMPTY, PENDING_QUOTA, FAILED = "DONE", "EMPTY", "PENDING_QUOTA", "FAILED"
SKIP_STATUSES = frozenset({DONE, EMPTY})

# Fields are in migration column order (a test compares them with the SQL file).
TickRow = namedtuple(
    "TickRow",
    "source product query_code contract_code trading_day session exch_ts seq "
    "price_scaled volume bid_scaled ask_scaled tick_type ingest_ts",
)
KbarRow = namedtuple(
    "KbarRow",
    "source product query_code contract_code timeframe_min session bar_ts "
    "open_scaled high_scaled low_scaled close_scaled volume ingest_ts",
)
WatermarkRow = namedtuple(
    "WatermarkRow",
    "source product kind trading_day status rows contract_code usage_bytes_after note updated_ts",
)


class SourceError(RuntimeError):
    """A broker query failed or returned something unusable; the affected days are marked FAILED."""


class IntradayRefusedError(RuntimeError):
    """Shioaji tick queries were refused because the TAIFEX day session is (about to be) open."""


SHIOAJI_KBAR_NS = 60 * 1_000_000_000
MONTH_LETTERS = "ABCDEFGHIJKL"


def settlement_day(year: int, month: int) -> dt.date:
    """TAIFEX monthly futures last trading day: the third Wednesday (holiday shifts are not modelled)."""
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(2 - first.weekday()) % 7 + 14)


def front_month_code(product: str, trading_day: dt.date) -> str:
    """Delivery-month code R1 pointed at on trading_day, e.g. TMF + 2026-11-20 -> TMFL6 (after the 11-18 roll)."""
    year, month = trading_day.year, trading_day.month
    if trading_day > settlement_day(year, month):
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{product}{MONTH_LETTERS[month - 1]}{year % 10}"


def scale_price(value: Any) -> int:
    """Exact x1,000,000 conversion through the decimal string; never float multiplication."""
    return int(round(Decimal(str(value)) * PRICE_SCALE))


def local_ns_to_utc_ns(ts_local_ns: int) -> int:
    """Shioaji historical ``ts`` (local Taipei epoch ns) to UTC epoch ns."""
    return int(ts_local_ns) - TAIPEI_OFFSET_NS


def iso_to_utc_ns(text: str) -> int:
    """ISO-8601 timestamp WITH an offset to UTC epoch ns; a naive timestamp is refused."""
    parsed = dt.datetime.fromisoformat(str(text))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp without a timezone offset: {text!r}")
    delta = parsed - _EPOCH
    return (delta.days * 86400 + delta.seconds) * NS_PER_S + delta.microseconds * 1000


def taipei_date(utc_ns: int) -> dt.date:
    return (_EPOCH + dt.timedelta(microseconds=utc_ns // 1000)).astimezone(TAIPEI).date()


def classify_session(utc_ns: int) -> str:
    """Taipei 08:45 <= t < 14:00 is the day session (13:45 close plus margin); everything else is night."""
    minute_of_day = ((utc_ns + TAIPEI_OFFSET_NS) // (60 * NS_PER_S)) % 1440
    return "day" if DAY_OPEN_MIN <= minute_of_day < DAY_CLOSE_MIN else "night"


def weekdays(start: dt.date, end: dt.date) -> list[dt.date]:
    """Mon-Fri dates in [start, end], ascending. Exchange holidays are unknown here (they end up EMPTY)."""
    days = (start + dt.timedelta(days=i) for i in range((end - start).days + 1))
    return [d for d in days if d.weekday() < 5]


def last_completed_trading_day(now_local: dt.datetime) -> dt.date:
    """Latest weekday whose day session has closed (14:00 Taipei) as of ``now_local``."""
    day = now_local.date() if now_local.time() >= dt.time(14) else now_local.date() - dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def intraday_tick_cap(now_local: dt.datetime, allow_intraday: bool, max_tick_days: int) -> int:
    """Shioaji tick-call cap; refuses on a weekday 08:30-13:50 Taipei unless allowed (then capped at 8)."""
    if now_local.weekday() < 5 and (8, 30) <= (now_local.hour, now_local.minute) <= (13, 50):
        if not allow_intraday:
            raise IntradayRefusedError(
                "Shioaji tick queries refused 08:30-13:50 Taipei on a weekday (--allow-intraday)"
            )
        return min(max_tick_days, INTRADAY_MAX_TICK_DAYS)
    return max_tick_days
