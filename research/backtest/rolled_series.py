"""Rolled front-month series for maker Gate C (ADR-004, option A).

A single TMF contract month is liquid for about a month, so no single-contract
run can reach a 60-day sample. A candidate whose manifest instrument is
``<ROOT>_frontmonth`` is judged instead on one series that rolls across contract
months by the pre-registered rule in
``config/research/protocols/front_month_roll_v1.yaml``. Every value that rule
states is checked here against what this module implements; a protocol that
says anything else is refused rather than run under a different rule.

The rule matches the live R1 binding (``build_snapshot_from_calendar`` with
``taifex_delivery_cutoff``): trading day D quotes the contract with the
earliest delivery date on or after D, and D covers the night session that
opens after the previous day session, then D's day session. On a delivery day
the expiring contract stops trading at 13:30 and live R1 moves to the next
month at that instant, so that day is split into two segments, each on its own
contract.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from hft_platform.alpha.paths import repo_path

PROTOCOL_SCHEMA = "research_protocol.v1"
DEFAULT_PROTOCOL_PATH = Path("config/research/protocols/front_month_roll_v1.yaml")
ROLLED_SUFFIX = "_frontmonth"

# The values of the protocol this module implements. Anything else is refused.
_IMPLEMENTED = {
    "kind": "roll_rule",
    "calendar": "XTAI",
    "timezone": "Asia/Taipei",
    "delivery_date": "third_wednesday_next_trading_day",
    "quoted_contract": "earliest_delivery_on_or_after_trading_day",
    "month_codes": "ABCDEFGHIJKL",
    "delivery_cutoff_local": "13:30",
    "roll_day_pnl": "per_segment_on_quoted_contract",
}
_IMPLEMENTED_TRADING_DAY = {"night_session": "next_trading_day", "window_start_local": "14:00"}
_ROOT = re.compile(r"^[A-Z]{2,4}$")


class RollProtocolError(ValueError):
    """The roll protocol is malformed or states a rule this module does not implement."""


@dataclass(frozen=True, slots=True)
class RollProtocol:
    protocol_id: str
    sha256: str
    path: str
    window_start: dt.time
    tz: ZoneInfo
    month_codes: str
    delivery_cutoff: dt.time


@dataclass(frozen=True, slots=True)
class RolledSegment:
    """A part of a trading day on one contract: ``start_ns <= exch_ts < end_ns``."""

    symbol: str
    start_ns: int
    end_ns: int


@dataclass(frozen=True, slots=True)
class RolledDay:
    """One trading day of the series: one segment, or two on a delivery day."""

    trading_day: dt.date
    segments: tuple[RolledSegment, ...]

    @property
    def symbol(self) -> str:
        """The contract the day opens on."""
        return self.segments[0].symbol

    @property
    def start_ns(self) -> int:
        return self.segments[0].start_ns

    @property
    def end_ns(self) -> int:
        return self.segments[-1].end_ns


@dataclass(frozen=True, slots=True)
class RolledSchedule:
    instrument: str
    root: str
    protocol: RollProtocol
    days: tuple[RolledDay, ...]

    @property
    def contracts(self) -> tuple[str, ...]:
        seen: list[str] = []
        for day in self.days:
            for segment in day.segments:
                if not seen or seen[-1] != segment.symbol:
                    seen.append(segment.symbol)
        return tuple(seen)


class SessionCalendar:
    """Trading sessions from exchange_calendars, with no weekday fallback.

    ``hft_platform.core.market_calendar.MarketCalendar`` guesses weekdays when
    exchange_calendars is missing or a date is out of range; a guessed holiday
    would move a roll, so here both are errors.
    """

    __slots__ = ("_cal", "_name")

    def __init__(self, name: str) -> None:
        try:
            import exchange_calendars as xcals
        except ImportError as exc:  # pragma: no cover - a pinned dependency
            raise RollProtocolError("exchange_calendars is not installed; the roll calendar is unavailable") from exc
        self._name = name
        self._cal = xcals.get_calendar(name)

    def _in_range(self, day: dt.date) -> None:
        first = self._cal.first_session.date()
        last = self._cal.last_session.date()
        if not first <= day <= last:
            raise RollProtocolError(f"{day} is outside the {self._name} calendar ({first}..{last})")

    def is_session(self, day: dt.date) -> bool:
        self._in_range(day)
        return bool(self._cal.is_session(day.isoformat()))

    def next_session(self, day: dt.date) -> dt.date:
        """The first session after ``day``."""
        self._in_range(day)
        return self._cal.date_to_session((day + dt.timedelta(days=1)).isoformat(), direction="next").date()

    def previous_session(self, day: dt.date) -> dt.date:
        """The last session before ``day``."""
        self._in_range(day)
        return self._cal.date_to_session((day - dt.timedelta(days=1)).isoformat(), direction="previous").date()


def load_roll_protocol(path: str | Path | None = None) -> RollProtocol:
    """Read and check a roll protocol; its sha256 is over the file's bytes."""
    resolved = repo_path(DEFAULT_PROTOCOL_PATH) if path is None else Path(path)
    raw = resolved.read_bytes()
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RollProtocolError(f"{resolved}: not YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise RollProtocolError(f"{resolved}: expected a mapping")
    if doc.get("schema") != PROTOCOL_SCHEMA:
        raise RollProtocolError(f"{resolved}: schema must be {PROTOCOL_SCHEMA!r}, got {doc.get('schema')!r}")
    protocol_id = doc.get("id")
    if not isinstance(protocol_id, str) or not protocol_id:
        raise RollProtocolError(f"{resolved}: id is required")
    for key, expected in _IMPLEMENTED.items():
        if doc.get(key) != expected:
            raise RollProtocolError(f"{resolved}: {key}={doc.get(key)!r} is not implemented (expected {expected!r})")
    trading_day = doc.get("trading_day")
    if not isinstance(trading_day, dict):
        raise RollProtocolError(f"{resolved}: trading_day must be a mapping")
    for key, expected in _IMPLEMENTED_TRADING_DAY.items():
        if trading_day.get(key) != expected:
            raise RollProtocolError(
                f"{resolved}: trading_day.{key}={trading_day.get(key)!r} is not implemented (expected {expected!r})"
            )
    return RollProtocol(
        protocol_id=protocol_id,
        sha256=hashlib.sha256(raw).hexdigest(),
        path=str(resolved),
        window_start=dt.time.fromisoformat(_IMPLEMENTED_TRADING_DAY["window_start_local"]),
        tz=ZoneInfo(_IMPLEMENTED["timezone"]),
        month_codes=_IMPLEMENTED["month_codes"],
        delivery_cutoff=dt.time.fromisoformat(_IMPLEMENTED["delivery_cutoff_local"]),
    )


def rolled_root(instrument: str) -> str | None:
    """``TMF`` for ``TMF_frontmonth``; None when the instrument is not a rolled series."""
    if not instrument.endswith(ROLLED_SUFFIX):
        return None
    root = instrument[: -len(ROLLED_SUFFIX)]
    if not _ROOT.match(root):
        raise RollProtocolError(f"rolled instrument {instrument!r}: root must be 2-4 capital letters")
    return root


def contract_code(root: str, year: int, month: int, protocol: RollProtocol) -> str:
    return f"{root}{protocol.month_codes[month - 1]}{year % 10}"


def delivery_date(year: int, month: int, calendar: SessionCalendar) -> dt.date:
    """Third Wednesday of the month, or the next trading day when it is not one."""
    first = dt.date(year, month, 1)
    third_wednesday = first + dt.timedelta(days=(2 - first.weekday()) % 7 + 14)
    if calendar.is_session(third_wednesday):
        return third_wednesday
    return calendar.next_session(third_wednesday)


def _quoted_month(trading_day: dt.date, calendar: SessionCalendar) -> tuple[int, int]:
    year, month = trading_day.year, trading_day.month
    if delivery_date(year, month, calendar) < trading_day:
        year, month = _next_month(year, month)
    return year, month


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def quoted_contract(root: str, trading_day: dt.date, protocol: RollProtocol, calendar: SessionCalendar) -> str:
    """The contract with the earliest delivery date on or after ``trading_day``."""
    return contract_code(root, *_quoted_month(trading_day, calendar), protocol)


def day_segments(
    root: str, trading_day: dt.date, protocol: RollProtocol, calendar: SessionCalendar
) -> tuple[RolledSegment, ...]:
    """The trading day's window on the contract live R1 binds at each instant.

    On the quoted contract's delivery day the window splits at the delivery
    cutoff (13:30): the expiring contract until then, the next month after,
    as ``taifex_delivery_cutoff`` does live.
    """
    start_ns, end_ns = trading_day_window(trading_day, protocol, calendar)
    year, month = _quoted_month(trading_day, calendar)
    symbol = contract_code(root, year, month, protocol)
    if delivery_date(year, month, calendar) != trading_day:
        return (RolledSegment(symbol, start_ns, end_ns),)
    cutoff_ns = _epoch_ns(dt.datetime.combine(trading_day, protocol.delivery_cutoff, tzinfo=protocol.tz))
    following = contract_code(root, *_next_month(year, month), protocol)
    return (RolledSegment(symbol, start_ns, cutoff_ns), RolledSegment(following, cutoff_ns, end_ns))


def trading_day_window(trading_day: dt.date, protocol: RollProtocol, calendar: SessionCalendar) -> tuple[int, int]:
    """``[previous trading day 14:00, trading_day 14:00)`` local, as epoch ns."""
    previous = calendar.previous_session(trading_day)
    start = dt.datetime.combine(previous, protocol.window_start, tzinfo=protocol.tz)
    end = dt.datetime.combine(trading_day, protocol.window_start, tzinfo=protocol.tz)
    return _epoch_ns(start), _epoch_ns(end)


def build_schedule(
    instrument: str,
    trading_days: Iterable[dt.date],
    *,
    protocol: RollProtocol | None = None,
    calendar: SessionCalendar | None = None,
) -> RolledSchedule:
    """The rolled series over ``trading_days``; a non-session day is an error."""
    root = rolled_root(instrument)
    if root is None:
        raise RollProtocolError(f"{instrument!r} is not a rolled instrument (expected <ROOT>{ROLLED_SUFFIX})")
    protocol = protocol or load_roll_protocol()
    calendar = calendar or SessionCalendar(_IMPLEMENTED["calendar"])
    days: list[RolledDay] = []
    for day in sorted(set(trading_days)):
        if not calendar.is_session(day):
            raise RollProtocolError(f"{day} is not a {_IMPLEMENTED['calendar']} trading day")
        days.append(RolledDay(day, day_segments(root, day, protocol, calendar)))
    if not days:
        raise ValueError(f"No trading days for {instrument}")
    return RolledSchedule(instrument=instrument, root=root, protocol=protocol, days=tuple(days))


def schedule_from_source(
    instrument: str,
    ck_source: Any,
    *,
    protocol: RollProtocol | None = None,
    calendar: SessionCalendar | None = None,
) -> RolledSchedule:
    """Every trading day whose window holds an event of any ``<ROOT>`` contract month.

    An event is dated by the window it falls in, so a Friday night session and
    its Saturday-morning tail belong to Monday.
    """
    root = rolled_root(instrument)
    if root is None:
        raise RollProtocolError(f"{instrument!r} is not a rolled instrument (expected <ROOT>{ROLLED_SUFFIX})")
    protocol = protocol or load_roll_protocol()
    calendar = calendar or SessionCalendar(_IMPLEMENTED["calendar"])
    slots = ck_source.available_local_slots(root, protocol.tz.key, _minute_of_day(protocol.window_start))
    return build_schedule(
        instrument,
        trading_days_of_slots(slots, calendar),
        protocol=protocol,
        calendar=calendar,
    )


def trading_days_of_slots(slots: Iterable[tuple[dt.date, bool]], calendar: SessionCalendar) -> list[dt.date]:
    """The trading day of each ``(local date, at or after the window start)`` slot holding data.

    An event before the window start on a session belongs to that session.
    Any other (the afternoon and night after a session, or a weekend or
    holiday) belongs to the next session.
    """
    days = {day if not late and calendar.is_session(day) else calendar.next_session(day) for day, late in slots}
    return sorted(days)


def _minute_of_day(moment: dt.time) -> int:
    return moment.hour * 60 + moment.minute


def _epoch_ns(moment: dt.datetime) -> int:
    return int(moment.timestamp()) * 1_000_000_000
