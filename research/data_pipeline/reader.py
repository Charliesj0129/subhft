"""The one ClickHouse reader of the governed research path (research v2 C1).

Governed consumers (MakerEngine's ``ClickHouseSource``, the queue calibration) read
``hft.market_data`` through :class:`GovernedReader`, which applies, here and nowhere else:

1. one query per window: a symbol and ``start_ns <= exch_ts < end_ns``, ordered by
   ``exch_ts, ingest_ts, seq_no`` so rows sharing an exchange timestamp keep arrival order;
2. the session rule (:func:`research.data_pipeline.is_session_row`): the TAIFEX clock windows
   plus the XTAI calendar, with no weekday fallback (a missing calendar is an error);
3. duplicate-delivery removal: ``dedup.RULE_ID`` for ticks, the snapshot rule for BidAsk.

Trading day D is the window ``[previous session 14:00, D 14:00)`` Asia/Taipei, the rule of
``config/research/protocols/front_month_roll_v1.yaml``, so a night session belongs to the next
trading day. Prices stay at the ClickHouse scale ``CK_PRICE_SCALE`` (x1,000,000); a consumer
converts explicitly. Every read returns its own counts so the evidence can say what was removed.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from numpy.typing import NDArray

from research.backtest import rolled_series
from research.data_pipeline import asof, dedup, filter_session_rows

READER_ID = "governed_ck_reader_v1"
CK_PRICE_SCALE = 1_000_000
# Same window as the exporter's BidAsk rule (``research.data_pipeline.DEDUP_WINDOW_NS``).
BIDASK_DEDUP_WINDOW_NS = 500_000
SESSION_RULE = "xtai_session_dates+taifex_session_windows"
QUERY_SETTINGS = {"max_memory_usage": 2_500_000_000, "max_threads": 2}

# Row layout: the exporter's nine columns, then ``trade_direction``.
COLUMNS = (
    "type",
    "exch_ts",
    "local_ts",
    "bids_price",
    "asks_price",
    "bids_vol",
    "asks_vol",
    "price_scaled",
    "volume",
    "trade_direction",
)

_WINDOW_SQL = """
    SELECT type, exch_ts, ingest_ts AS local_ts, bids_price, asks_price, bids_vol, asks_vol,
           price_scaled, volume, trade_direction
    FROM hft.market_data
    WHERE symbol = %(symbol)s AND exch_ts >= %(start_ns)s AND exch_ts < %(end_ns)s
    ORDER BY exch_ts, ingest_ts, seq_no
"""
_SLOTS_SQL = """
    SELECT DISTINCT
        toDate(fromUnixTimestamp64Nano(exch_ts), %(tz)s) AS d,
        toHour(fromUnixTimestamp64Nano(exch_ts), %(tz)s) * 60
            + toMinute(fromUnixTimestamp64Nano(exch_ts), %(tz)s) >= %(split_minute)s AS late
    FROM hft.market_data
    WHERE {match}
    ORDER BY d, late
"""
_ROOT = re.compile(r"^[A-Z]{2,4}$")
_SYMBOL = re.compile(r"^[A-Z0-9]{2,16}$")
_TAIPEI = ZoneInfo("Asia/Taipei")


class ReaderError(RuntimeError):
    """The governed reader cannot produce a faithful read (no calendar, bad window, bad symbol)."""


@dataclass(frozen=True, slots=True)
class WindowRead:
    """The rows of one symbol and window after the session rule and dedup, with what was removed."""

    symbol: str
    start_ns: int
    end_ns: int
    rows: tuple[tuple[Any, ...], ...]
    rows_fetched: int
    session_filtered: int
    tick_dedup: dedup.DedupStats
    bidask_removed: int
    depth_counts: dict[int, int] = field(default_factory=dict)

    def quotes(self) -> list[tuple[Any, ...]]:
        """BidAsk rows with at least one level on each side, in row order."""
        return [r for r in self.rows if r[0] == "BidAsk" and r[3] and r[4]]

    def fresh_quote_index(
        self, queries: NDArray[np.int64], *, max_age_ns: int = asof.DEFAULT_MAX_AGE_NS
    ) -> NDArray[np.int64]:
        """Index into :meth:`quotes` of the quote in force at each query, ``asof.NO_QUOTE`` when stale."""
        times = np.fromiter((int(r[1]) for r in self.quotes()), dtype=np.int64)
        return asof.asof_index(times, np.asarray(queries, dtype=np.int64), max_age_ns=max_age_ns)

    def provenance(self) -> dict[str, Any]:
        return {
            "reader": READER_ID,
            "symbol": self.symbol,
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "rows_fetched": self.rows_fetched,
            "rows_out": len(self.rows),
            "session_rule": SESSION_RULE,
            "session_filtered_rows": self.session_filtered,
            "dedup_rule": dedup.RULE_ID,
            "tick_dedup": self.tick_dedup.to_payload(),
            "bidask_removed": self.bidask_removed,
            "bidask_depth_counts": {str(k): v for k, v in sorted(self.depth_counts.items())},
        }


class GovernedReader:
    """Read ``hft.market_data`` for governed research. ``client`` is a ``clickhouse_connect`` client."""

    __slots__ = ("_client", "_connect", "_calendar", "_protocol")

    def __init__(
        self,
        client: Any = None,
        *,
        connect: Callable[[], Any] | None = None,
        calendar: rolled_series.SessionCalendar | None = None,
        protocol: rolled_series.RollProtocol | None = None,
    ) -> None:
        if (client is None) == (connect is None):
            raise ReaderError("give exactly one of client or connect")
        self._client = client
        self._connect = connect
        self._calendar = calendar
        self._protocol = protocol

    @classmethod
    def connect(cls, *, host: str, port: int, user: str, password: str) -> GovernedReader:
        """A reader that opens its ClickHouse HTTP client on the first query."""

        def _open() -> Any:
            import clickhouse_connect  # noqa: PLC0415 - optional at import time, like the exporter

            return clickhouse_connect.get_client(host=host, port=port, username=user, password=password)

        return cls(connect=_open)

    def _query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        if self._client is None:
            assert self._connect is not None
            self._client = self._connect()
        return self._client.query(sql, parameters=parameters or {}, settings=QUERY_SETTINGS)

    def _get_calendar(self) -> rolled_series.SessionCalendar:
        if self._calendar is None:
            try:
                self._calendar = rolled_series.SessionCalendar("XTAI")
            except rolled_series.RollProtocolError as exc:
                raise ReaderError(f"no exchange calendar; the session rule cannot be applied: {exc}") from exc
        return self._calendar

    def _get_protocol(self) -> rolled_series.RollProtocol:
        if self._protocol is None:
            self._protocol = rolled_series.load_roll_protocol()
        return self._protocol

    def ping(self) -> None:
        self._query("SELECT 1")

    def _sessions(self, start_ns: int, end_ns: int) -> frozenset[str]:
        """Session dates that can own a row in the window (one day left for the night tail)."""
        calendar = self._get_calendar()
        first = _taipei_date(start_ns) - dt.timedelta(days=1)
        last = _taipei_date(max(start_ns, end_ns - 1))
        out: set[str] = set()
        day = first
        while day <= last:
            try:
                if calendar.is_session(day):
                    out.add(day.isoformat())
            except rolled_series.RollProtocolError as exc:
                raise ReaderError(str(exc)) from exc
            day += dt.timedelta(days=1)
        return frozenset(out)

    def read_window(self, symbol: str, start_ns: int, end_ns: int) -> WindowRead:
        """Rows of ``symbol`` with ``start_ns <= exch_ts < end_ns`` after the session rule and dedup."""
        if not _SYMBOL.match(symbol):
            raise ReaderError(f"not a contract symbol: {symbol!r}")
        if int(end_ns) <= int(start_ns):
            raise ReaderError(f"empty window [{start_ns}, {end_ns})")
        sessions = self._sessions(int(start_ns), int(end_ns))
        result = self._query(_WINDOW_SQL, {"symbol": symbol, "start_ns": int(start_ns), "end_ns": int(end_ns)})
        fetched = [tuple(r) for r in result.result_rows]
        in_session, session_filtered = filter_session_rows(fetched, sessions)
        rows, tick_stats = dedup.dedup_tick_rows(in_session)
        rows, bidask_removed = dedup.dedup_bidask_rows(rows, window_ns=BIDASK_DEDUP_WINDOW_NS)
        depth = Counter(min(len(r[3] or ()), len(r[4] or ())) for r in rows if r[0] == "BidAsk")
        return WindowRead(
            symbol=symbol,
            start_ns=int(start_ns),
            end_ns=int(end_ns),
            rows=tuple(tuple(r) for r in rows),
            rows_fetched=len(fetched),
            session_filtered=session_filtered,
            tick_dedup=tick_stats,
            bidask_removed=bidask_removed,
            depth_counts=dict(depth),
        )

    def trading_day_window(self, trading_day: dt.date) -> tuple[int, int]:
        """``[previous session 14:00, trading_day 14:00)`` Asia/Taipei; a non-session day is an error."""
        calendar = self._get_calendar()
        try:
            if not calendar.is_session(trading_day):
                raise ReaderError(f"{trading_day} is not an XTAI trading day")
            return rolled_series.trading_day_window(trading_day, self._get_protocol(), calendar)
        except rolled_series.RollProtocolError as exc:
            raise ReaderError(str(exc)) from exc

    def read_trading_day(self, symbol: str, trading_day: dt.date) -> WindowRead:
        """The night session before ``trading_day`` and its day session, as one read."""
        start_ns, end_ns = self.trading_day_window(trading_day)
        return self.read_window(symbol, start_ns, end_ns)

    def local_slots(
        self, *, tz: str, split_minute: int, root: str | None = None, symbol: str | None = None
    ) -> list[tuple[dt.date, bool]]:
        """``(local date, at or after split_minute)`` pairs holding an event of ``symbol`` or any ``root`` month."""
        if (root is None) == (symbol is None):
            raise ReaderError("give exactly one of root or symbol")
        if root is not None and not _ROOT.match(root):
            raise ReaderError(f"contract root must be 2-4 capital letters, got {root!r}")
        if symbol is not None and not _SYMBOL.match(symbol):
            raise ReaderError(f"not a contract symbol: {symbol!r}")
        if not 0 <= int(split_minute) < 24 * 60:
            raise ReaderError(f"split_minute must be a minute of the day, got {split_minute!r}")
        match = "symbol = %(symbol)s" if symbol is not None else "match(symbol, %(pattern)s)"
        parameters: dict[str, Any] = {"tz": tz, "split_minute": int(split_minute)}
        if symbol is not None:
            parameters["symbol"] = symbol
        else:
            parameters["pattern"] = f"^{root}[A-L][0-9]$"
        result = self._query(_SLOTS_SQL.format(match=match), parameters)
        return [(_as_date(r[0]), bool(int(r[1]))) for r in result.result_rows]

    def trading_days(self, symbol: str) -> list[dt.date]:
        """Every trading day whose window holds an event of ``symbol``."""
        protocol = self._get_protocol()
        split = protocol.window_start.hour * 60 + protocol.window_start.minute
        slots = self.local_slots(tz=protocol.tz.key, split_minute=split, symbol=symbol)
        try:
            return rolled_series.trading_days_of_slots(slots, self._get_calendar())
        except rolled_series.RollProtocolError as exc:
            raise ReaderError(str(exc)) from exc


def _taipei_date(ts_ns: int) -> dt.date:
    return dt.datetime.fromtimestamp(int(ts_ns) // 1_000_000_000, tz=_TAIPEI).date()


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


class GovernedHbtSource:
    """``ChDataSource.load_day`` over the governed reader: one trading day as hftbacktest events.

    The event assembly is ``ChDataSource``'s own (``assemble_day_events``); only the rows differ:
    trading-day window, session rule and dedup instead of a UTC calendar date with every copy.
    """

    __slots__ = ("_reader", "_reads")

    def __init__(self, reader: GovernedReader) -> None:
        self._reader = reader
        self._reads: list[dict[str, Any]] = []

    @property
    def reads(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._reads)

    def load_day(self, instrument: str, date: str) -> NDArray[Any]:
        import pandas as pd  # noqa: PLC0415 - offline calibration only

        from hft_platform.backtest.ch_data_source import (  # noqa: PLC0415
            DataValidationError,
            assemble_day_events,
            validate_events,
        )

        read = self._reader.read_trading_day(instrument, dt.date.fromisoformat(date))
        self._reads.append(read.provenance())
        if not read.rows:
            raise DataValidationError(f"{instrument} {date}: no in-session rows in hft.market_data")
        frame = pd.DataFrame(
            {
                "exch_ts": [int(r[1]) for r in read.rows],
                "local_ts": [int(r[2]) for r in read.rows],
                "event_type": [r[0] for r in read.rows],
                "price": [int(r[7] or 0) for r in read.rows],
                "volume": [int(r[8] or 0) for r in read.rows],
                "trade_direction": [int(r[9] or 0) for r in read.rows],
                "bid_prices": [list(r[3] or ()) for r in read.rows],
                "bid_volumes": [list(r[5] or ()) for r in read.rows],
                "ask_prices": [list(r[4] or ()) for r in read.rows],
                "ask_volumes": [list(r[6] or ()) for r in read.rows],
            }
        )
        events = assemble_day_events(frame, price_scale=CK_PRICE_SCALE)
        validate_events(events, instrument=instrument)
        return events

    def load_days(self, instrument: str, dates: list[str]) -> list[NDArray[Any]]:
        return [self.load_day(instrument, d) for d in dates]
