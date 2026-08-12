"""Official TAIFEX trading-date windows for governed research exports.

TAIFEX after-hours trading belongs to the next regular XTAI session.  Calendar
dates are therefore not valid substitutes for trading dates: Friday night
belongs to Monday, and the night before a market holiday belongs to the next
open session.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import numpy as np

CALENDAR_NAME = "XTAI"
CALENDAR_PACKAGE = "exchange-calendars"
QUERY_START_TIME = time(15, 0)
QUERY_END_TIME = time(14, 0)
DAY_SESSION_OPEN_MINUTE = 8 * 60 + 45
DAY_SESSION_CLOSE_MINUTE = 13 * 60 + 45
NIGHT_SESSION_OPEN_MINUTE = 15 * 60
NIGHT_SESSION_CLOSE_MINUTE = 5 * 60
TAIFEX_FUTURES_ROOTS: tuple[str, ...] = ("TMF", "TXF")
TAIFEX_FUTURES_MONTH_CODES = "ABCDEFGHIJKL"


class TradingDateGovernanceError(RuntimeError):
    """Raised when a requested window is not an official XTAI session window."""


@dataclass(frozen=True, slots=True)
class TradingDateWindow:
    """Frozen official-session mapping and raw wall-clock query bounds."""

    requested_date_from: str
    requested_date_to: str
    expected_trading_dates: tuple[str, ...]
    warmup_trading_date: str
    query_wall_time_from: str
    query_wall_time_to: str
    night_open_date_mapping: tuple[tuple[str, str], ...]
    calendar_name: str
    calendar_package_version: str
    calendar_mapping_hash: str

    @property
    def query_date_from(self) -> str:
        return self.query_wall_time_from.split(" ", maxsplit=1)[0]

    @property
    def query_date_to(self) -> str:
        return self.query_wall_time_to.split(" ", maxsplit=1)[0]


@dataclass(frozen=True, slots=True)
class FullSessionEligibility:
    """Strict 60-minute completeness evidence for a requested window."""

    expected_trading_dates: tuple[str, ...]
    eligible_trading_dates: tuple[str, ...]
    missing_expected_trading_dates: tuple[str, ...]
    excluded_partial_trading_dates: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class BarAggregationLayout:
    """Column indexes needed to causally combine already-aggregated bars."""

    open_index: int
    high_index: int
    low_index: int
    close_index: int
    sum_indices: tuple[int, ...]
    first_indices: tuple[int, ...]
    last_indices: tuple[int, ...]


def _iso_session(value: object) -> str:
    return str(value)[:10]


def build_trading_date_window(date_from: str, date_to: str) -> TradingDateWindow:
    """Build a deterministic official XTAI session window.

    One complete trading day before ``date_from`` is included as warm-up so
    front-contract selection for the first requested day uses only prior-day
    activity.  The warm-up day's night session itself opens on the preceding
    XTAI session, which defines the raw query start.
    """

    requested_start = date.fromisoformat(date_from)
    requested_end = date.fromisoformat(date_to)
    if requested_end < requested_start:
        raise TradingDateGovernanceError("date_to must be on or after date_from")

    calendar = xcals.get_calendar(CALENDAR_NAME)
    expected = tuple(_iso_session(value) for value in calendar.sessions_in_range(date_from, date_to))
    if not expected or expected[0] != date_from or expected[-1] != date_to:
        raise TradingDateGovernanceError(
            f"requested bounds must both be official {CALENDAR_NAME} sessions: {date_from} -> {date_to}"
        )

    warmup = _iso_session(calendar.previous_session(date_from))
    query_open_date = _iso_session(calendar.previous_session(warmup))
    query_start = datetime.combine(date.fromisoformat(query_open_date), QUERY_START_TIME)
    query_end = datetime.combine(requested_end, QUERY_END_TIME)

    mapping: list[tuple[str, str]] = []
    current = query_start.date()
    while current <= query_end.date():
        next_session = _iso_session(calendar.date_to_session(current.isoformat(), direction="next"))
        if next_session == current.isoformat():
            next_session = _iso_session(calendar.next_session(next_session))
        mapping.append((current.isoformat(), next_session))
        current += timedelta(days=1)

    package_version = importlib.metadata.version(CALENDAR_PACKAGE)
    mapping_payload = {
        "calendar_name": CALENDAR_NAME,
        "calendar_package_version": package_version,
        "expected_trading_dates": expected,
        "night_open_date_mapping": mapping,
        "query_wall_time_from": query_start.isoformat(sep=" "),
        "query_wall_time_to": query_end.isoformat(sep=" "),
        "requested_date_from": date_from,
        "requested_date_to": date_to,
        "warmup_trading_date": warmup,
    }
    mapping_hash = hashlib.sha256(
        json.dumps(mapping_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return TradingDateWindow(
        requested_date_from=date_from,
        requested_date_to=date_to,
        expected_trading_dates=expected,
        warmup_trading_date=warmup,
        query_wall_time_from=query_start.isoformat(sep=" "),
        query_wall_time_to=query_end.isoformat(sep=" "),
        night_open_date_mapping=tuple(mapping),
        calendar_name=CALENDAR_NAME,
        calendar_package_version=package_version,
        calendar_mapping_hash=mapping_hash,
    )


def clickhouse_trading_day_expression(event_time_expression: str, window: TradingDateWindow) -> str:
    """Return a deterministic ClickHouse expression for official trading dates."""

    day_session_predicate, night_session_predicate = clickhouse_taifex_session_predicates(event_time_expression)
    open_dates = ", ".join(f"'{source}'" for source, _target in window.night_open_date_mapping)
    trading_dates = ", ".join(f"'{target}'" for _source, target in window.night_open_date_mapping)
    day_dates = ", ".join(f"'{value}'" for value in (window.warmup_trading_date, *window.expected_trading_dates))
    return f"""multiIf(
        {night_session_predicate},
        toDate(transform(
            toString(if(
                toHour({event_time_expression}) >= 15,
                toDate({event_time_expression}),
                toDate({event_time_expression}) - 1
            )),
            [{open_dates}],
            [{trading_dates}],
            '1970-01-01'
        )),
        {day_session_predicate}
            AND has([{day_dates}], toString(toDate({event_time_expression}))),
        toDate({event_time_expression}),
        toDate('1970-01-01')
    )"""


def clickhouse_taifex_session_predicates(event_time_expression: str) -> tuple[str, str]:
    """Return exact TAIFEX day/night predicates for a Taipei-local timestamp."""

    minute_of_day = f"(toHour({event_time_expression}) * 60 + toMinute({event_time_expression}))"
    second = f"toSecond({event_time_expression})"
    day = f"""(
        {minute_of_day} >= {DAY_SESSION_OPEN_MINUTE}
        AND (
            {minute_of_day} < {DAY_SESSION_CLOSE_MINUTE}
            OR ({minute_of_day} = {DAY_SESSION_CLOSE_MINUTE} AND {second} = 0)
        )
    )"""
    night = f"""(
        {minute_of_day} >= {NIGHT_SESSION_OPEN_MINUTE}
        OR {minute_of_day} < {NIGHT_SESSION_CLOSE_MINUTE}
        OR ({minute_of_day} = {NIGHT_SESSION_CLOSE_MINUTE} AND {second} = 0)
    )"""
    return day, night


def clickhouse_taifex_bucket_timestamp(event_time_expression: str) -> str:
    """Keep closing prints in the final interval instead of a phantom bar."""

    minute_of_day = f"(toHour({event_time_expression}) * 60 + toMinute({event_time_expression}))"
    at_close = (
        f"({minute_of_day} IN ({DAY_SESSION_CLOSE_MINUTE}, {NIGHT_SESSION_CLOSE_MINUTE}) "
        f"AND toSecond({event_time_expression}) = 0)"
    )
    return f"if({at_close}, subtractSeconds({event_time_expression}, 1), {event_time_expression})"


def clickhouse_taifex_contract_predicate(symbol_expression: str = "symbol") -> str:
    """Return an indexable exact-symbol predicate for TAIFEX futures contracts."""

    symbols = (
        f"'{root}{month}{year}'"
        for root in TAIFEX_FUTURES_ROOTS
        for month in TAIFEX_FUTURES_MONTH_CODES
        for year in range(10)
    )
    return f"{symbol_expression} IN ({', '.join(symbols)})"


def reaggregate_taifex_bar_rows(
    rows: Sequence[Sequence[Any]],
    *,
    source_timeframe_min: int,
    target_timeframe_min: int,
    layout: BarAggregationLayout,
) -> list[tuple[Any, ...]]:
    """Derive a coarser TAIFEX bar lane without rescanning raw events."""

    source = int(source_timeframe_min)
    target = int(target_timeframe_min)
    if source < 1 or target <= source or target % source != 0:
        raise ValueError("target timeframe must be a larger integer multiple of source timeframe")
    highest_index = max(
        layout.open_index,
        layout.high_index,
        layout.low_index,
        layout.close_index,
        *layout.sum_indices,
        *layout.first_indices,
        *layout.last_indices,
    )
    taipei = ZoneInfo("Asia/Taipei")
    day_origin_ns = int(datetime(1970, 1, 1, 8, 45, tzinfo=taipei).timestamp() * 1_000_000_000)
    night_origin_ns = int(datetime(1970, 1, 1, 15, 0, tzinfo=taipei).timestamp() * 1_000_000_000)
    target_ns = target * 60 * 1_000_000_000
    groups: dict[tuple[str, str, str, str, int], list[Sequence[Any]]] = defaultdict(list)
    for row in rows:
        if len(row) <= highest_index:
            raise ValueError("bar row does not satisfy the aggregation layout")
        root, contract, trading_day, session = map(str, row[:4])
        timestamp = int(row[4])
        if target == 1440:
            target_session = "full"
            target_bucket = int(datetime.fromisoformat(trading_day).replace(tzinfo=taipei).timestamp() * 1_000_000_000)
        else:
            if session == "day":
                origin = day_origin_ns
            elif session == "night":
                origin = night_origin_ns
            else:
                raise ValueError(f"cannot intraday-reaggregate session {session!r}")
            target_session = session
            target_bucket = origin + ((timestamp - origin) // target_ns) * target_ns
        groups[(root, contract, trading_day, target_session, target_bucket)].append(row)

    aggregated: list[tuple[Any, ...]] = []
    for key, grouped_rows in groups.items():
        ordered = sorted(grouped_rows, key=lambda row: int(row[4]))
        first, last = ordered[0], ordered[-1]
        output = list(first)
        output[3] = key[3]
        output[4] = key[4]
        output[layout.open_index] = first[layout.open_index]
        output[layout.high_index] = max(row[layout.high_index] for row in ordered)
        output[layout.low_index] = min(row[layout.low_index] for row in ordered)
        output[layout.close_index] = last[layout.close_index]
        for index in layout.sum_indices:
            output[index] = sum(row[index] for row in ordered)
        for index in layout.first_indices:
            output[index] = first[index]
        for index in layout.last_indices:
            output[index] = last[index]
        aggregated.append(tuple(output))
    aggregated.sort(key=lambda row: (str(row[0]), str(row[2]), str(row[3]), int(row[4]), str(row[1])))
    return aggregated


def official_trading_date(wall_time: datetime, window: TradingDateWindow) -> str | None:
    """Map a Taipei-naive wall time to its official trading date for tests/audits."""

    wall_date = wall_time.date()
    seconds = wall_time.hour * 3600 + wall_time.minute * 60 + wall_time.second + wall_time.microsecond / 1_000_000
    night_open = NIGHT_SESSION_OPEN_MINUTE * 60
    night_close_exclusive = NIGHT_SESSION_CLOSE_MINUTE * 60 + 1
    day_open = DAY_SESSION_OPEN_MINUTE * 60
    day_close_exclusive = DAY_SESSION_CLOSE_MINUTE * 60 + 1
    if seconds >= night_open or seconds < night_close_exclusive:
        open_date = wall_date if wall_time.hour >= 15 else wall_date - timedelta(days=1)
        return dict(window.night_open_date_mapping).get(open_date.isoformat())
    if not (day_open <= seconds < day_close_exclusive):
        return None
    value = wall_date.isoformat()
    allowed = {window.warmup_trading_date, *window.expected_trading_dates}
    return value if value in allowed else None


def full_session_eligibility(
    *,
    root: Sequence[object] | np.ndarray,
    timeframe_min: Sequence[int] | np.ndarray,
    trading_day: Sequence[object] | np.ndarray,
    session: Sequence[object] | np.ndarray,
    expected_trading_dates: Sequence[str],
    roots: Sequence[str],
    required_timeframes: Sequence[int] = (),
) -> FullSessionEligibility:
    """Require full 60-minute sessions and presence in every requested group."""

    root_values = np.asarray(root)
    timeframe_values = np.asarray(timeframe_min)
    day_values = np.asarray(trading_day)
    session_values = np.asarray(session)
    expected = tuple(str(value) for value in expected_trading_dates)
    required = tuple(sorted({int(value) for value in required_timeframes}))
    eligible: list[str] = []
    missing: list[str] = []
    excluded: list[dict[str, Any]] = []
    sixty_minute = timeframe_values == 60

    for day in expected:
        day_mask = sixty_minute & (day_values == day)
        if not np.any(day_mask):
            missing.append(day)
        root_evidence: list[dict[str, Any]] = []
        complete = True
        for root_name in roots:
            group = day_mask & (root_values == root_name)
            day_count = int(np.count_nonzero(group & (session_values == "day")))
            night_count = int(np.count_nonzero(group & (session_values == "night")))
            root_complete = day_count == 5 and night_count == 14
            complete &= root_complete
            root_evidence.append(
                {
                    "root": root_name,
                    "day_60m_bars": day_count,
                    "night_60m_bars": night_count,
                    "expected_day_60m_bars": 5,
                    "expected_night_60m_bars": 14,
                    "complete": root_complete,
                }
            )
        group_evidence: list[dict[str, Any]] = []
        for root_name in roots:
            for timeframe in required:
                count = int(
                    np.count_nonzero((day_values == day) & (root_values == root_name) & (timeframe_values == timeframe))
                )
                complete &= count > 0
                group_evidence.append(
                    {
                        "root": root_name,
                        "timeframe_min": timeframe,
                        "bar_count": count,
                        "present": count > 0,
                    }
                )
        if complete:
            eligible.append(day)
        else:
            full_session_complete = all(bool(item["complete"]) for item in root_evidence)
            excluded.append(
                {
                    "trading_date": day,
                    "reason": (
                        "incomplete_cross_timeframe_coverage"
                        if full_session_complete
                        else "incomplete_full_session_60m"
                    ),
                    "roots": root_evidence,
                    "root_timeframe_groups": group_evidence,
                }
            )

    return FullSessionEligibility(
        expected_trading_dates=expected,
        eligible_trading_dates=tuple(eligible),
        missing_expected_trading_dates=tuple(missing),
        excluded_partial_trading_dates=tuple(excluded),
    )
