"""Research v2 C1: the one governed ClickHouse reader and the consumers moved onto it.

The reader applies the session rule, dedup v1 and trading-day windows; ``ClickHouseSource``
(MakerEngine) and ``GovernedHbtSource`` (queue calibration) read through it. Parity: on a clean
fixture day each consumer produces, row for row, what its old reader produced; the differences
on a dirty day (out-of-session rows, duplicate deliveries, tie order, the day boundary) are
each pinned by a test below.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from hft_platform.backtest.ch_data_source import assemble_day_events
from research.backtest import maker_engine, rolled_series
from research.backtest.maker_engine import ClickHouseSource, TickData
from research.backtest.rolled_series import SessionCalendar, load_roll_protocol
from research.data_pipeline import asof, dedup
from research.data_pipeline.reader import (
    CK_PRICE_SCALE,
    READER_ID,
    GovernedHbtSource,
    GovernedReader,
    ReaderError,
)

_TZ = ZoneInfo("Asia/Taipei")
FRI = dt.date(2026, 3, 13)
SAT = dt.date(2026, 3, 14)
MON = dt.date(2026, 3, 16)
SYMBOL = "TMFC6"
BID = 33_000_000_000
ASK = 33_001_000_000


def _ns(day: dt.date, hh: int, mm: int = 0, ss: int = 0, ns: int = 0) -> int:
    return int(dt.datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=_TZ).timestamp()) * 1_000_000_000 + ns


def _bidask(ts: int, bid: int = BID, ask: int = ASK, *, local: int | None = None, depth: int = 1) -> tuple[Any, ...]:
    bids = [bid - i * 1_000_000 for i in range(depth)]
    asks = [ask + i * 1_000_000 for i in range(depth)]
    return ("BidAsk", ts, ts if local is None else local, bids, asks, [3] * depth, [4] * depth, 0, 0, 0)


def _tick(ts: int, price: int = BID, volume: int = 1, *, direction: int = 1, local: int | None = None) -> tuple:
    return ("Tick", ts, ts if local is None else local, [], [], [], [], price, volume, direction)


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class FakeClient:
    """``hft.market_data`` for one symbol: answers the reader's window and slot queries."""

    def __init__(self, rows: list[tuple[Any, ...]], symbol: str = SYMBOL) -> None:
        self.rows = rows
        self.symbol = symbol
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def query(self, sql: str, parameters: dict[str, Any] | None = None, settings: Any = None) -> _Result:
        params = dict(parameters or {})
        self.queries.append((sql, params))
        if "SELECT 1" in sql:
            return _Result([(1,)])
        if "DISTINCT" in sql:
            if "symbol" in params and params["symbol"] != self.symbol:
                return _Result([])
            if "pattern" in params and not re.match(params["pattern"], self.symbol):
                return _Result([])
            tz = ZoneInfo(params["tz"])
            slots = set()
            for row in self.rows:
                local = dt.datetime.fromtimestamp(row[1] // 1_000_000_000, tz=tz)
                slots.add((local.date().isoformat(), int(local.hour * 60 + local.minute >= params["split_minute"])))
            return _Result(sorted(slots))
        if params["symbol"] != self.symbol:
            return _Result([])
        picked = [
            (row[1], row[2], index, row)
            for index, row in enumerate(self.rows)
            if params["start_ns"] <= row[1] < params["end_ns"]
        ]
        return _Result([row for *_, row in sorted(picked, key=lambda item: item[:3])])


@pytest.fixture(scope="module")
def calendar() -> SessionCalendar:
    return SessionCalendar("XTAI")


@pytest.fixture(scope="module")
def protocol() -> Any:
    return load_roll_protocol()


def _reader(rows: list[tuple[Any, ...]], calendar: SessionCalendar, protocol: Any) -> GovernedReader:
    return GovernedReader(FakeClient(rows), calendar=calendar, protocol=protocol)


def _clean_monday() -> list[tuple[Any, ...]]:
    """Friday night then Monday day session, one quote and one trade a minute apart, no repeats."""
    rows: list[tuple[Any, ...]] = []
    for index, start in enumerate((_ns(FRI, 15, 0), _ns(MON, 8, 45))):
        for minute in range(30):
            ts = start + minute * 60_000_000_000
            rows.append(_bidask(ts, BID + minute * 1_000_000, ASK + minute * 1_000_000, depth=1 + index * 4))
            rows.append(_tick(ts + 1_000_000_000, BID + minute * 1_000_000, 1 + minute % 3))
    return rows


def _old_clickhouse_source_events(rows: list[tuple[Any, ...]], start_ns: int, end_ns: int) -> list[TickData]:
    """What the pre-C1 ``ClickHouseSource._load_events`` built from the same table."""
    ba = sorted(
        (r for r in rows if r[0] == "BidAsk" and start_ns <= r[1] < end_ns and len(r[3]) >= 1 and len(r[4]) >= 1),
        key=lambda r: r[1],
    )
    ticks = sorted((r for r in rows if r[0] == "Tick" and start_ns <= r[1] < end_ns), key=lambda r: r[1])
    events = [
        TickData(r[1], r[3][0], r[4][0], r[5][0], r[6][0], 0, 0, False, scale=CK_PRICE_SCALE) for r in ba
    ] + [TickData(r[1], 0, 0, 0, 0, r[7], r[8], True, scale=CK_PRICE_SCALE) for r in ticks]
    events.sort(key=lambda e: e.exch_ts)
    return events


# --- the reader ---------------------------------------------------------------------------


def test_trading_day_owns_the_night_session_before_it(calendar: SessionCalendar, protocol: Any) -> None:
    reader = _reader(_clean_monday(), calendar, protocol)

    assert reader.trading_day_window(MON) == (_ns(FRI, 14), _ns(MON, 14))
    read = reader.read_trading_day(SYMBOL, MON)

    assert read.rows_fetched == 120 and len(read.rows) == 120
    assert read.rows[0][1] == _ns(FRI, 15, 0)


def test_reader_refuses_a_non_session_trading_day(calendar: SessionCalendar, protocol: Any) -> None:
    with pytest.raises(ReaderError, match="not an XTAI trading day"):
        _reader([], calendar, protocol).read_trading_day(SYMBOL, SAT)


def test_out_of_session_rows_are_dropped_and_counted(calendar: SessionCalendar, protocol: Any) -> None:
    rows = _clean_monday() + [
        _tick(_ns(FRI, 14, 30)),  # between the day close and the night open
        _tick(_ns(SAT, 9, 0)),  # day window on a closed date
        _tick(_ns(MON, 13, 50)),  # after the day close
    ]
    read = _reader(rows, calendar, protocol).read_trading_day(SYMBOL, MON)

    assert read.rows_fetched == 123
    assert read.session_filtered == 3
    assert len(read.rows) == 120


def test_night_tail_after_midnight_belongs_to_the_session_that_opened_it(
    calendar: SessionCalendar, protocol: Any
) -> None:
    rows = _clean_monday() + [_tick(_ns(SAT, 4, 59)), _tick(_ns(SAT, 5, 1))]
    read = _reader(rows, calendar, protocol).read_trading_day(SYMBOL, MON)

    assert read.session_filtered == 1  # 05:01 is after the night close; 04:59 is Friday's night
    assert any(r[1] == _ns(SAT, 4, 59) for r in read.rows)


def test_duplicate_tick_deliveries_are_removed_by_the_multiplicity_rule(
    calendar: SessionCalendar, protocol: Any
) -> None:
    clean = _clean_monday()
    doubled = [row for row in clean for _ in range(2 if row[0] == "Tick" else 1)]
    read = _reader(doubled, calendar, protocol).read_trading_day(SYMBOL, MON)

    assert read.tick_dedup.rule == dedup.RULE_ID
    assert read.tick_dedup.removed == 60
    assert [r for r in read.rows if r[0] == "Tick"] == [r for r in clean if r[0] == "Tick"]


def test_a_repeated_snapshot_at_one_timestamp_is_removed(calendar: SessionCalendar, protocol: Any) -> None:
    ts = _ns(MON, 9, 0, 30)
    rows = [_bidask(ts), _bidask(ts, local=ts + 5), _bidask(ts + 10_000_000_000)]
    read = _reader(rows, calendar, protocol).read_trading_day(SYMBOL, MON)

    assert read.bidask_removed == 1
    assert len(read.rows) == 2


def test_the_reader_fails_closed_without_a_calendar(monkeypatch: pytest.MonkeyPatch, protocol: Any) -> None:
    def _no_calendar(name: str) -> None:
        raise rolled_series.RollProtocolError("exchange_calendars is not installed")

    monkeypatch.setattr(rolled_series, "SessionCalendar", _no_calendar)
    reader = GovernedReader(FakeClient(_clean_monday()), protocol=protocol)

    with pytest.raises(ReaderError, match="no exchange calendar"):
        reader.read_window(SYMBOL, _ns(FRI, 14), _ns(MON, 14))


@pytest.mark.parametrize(
    ("symbol", "start", "end", "match"),
    [
        ("TMF'; DROP TABLE x", 0, 1, "not a contract symbol"),
        (SYMBOL, 10, 10, "empty window"),
    ],
)
def test_reader_refuses_a_bad_symbol_or_window(
    calendar: SessionCalendar, protocol: Any, symbol: str, start: int, end: int, match: str
) -> None:
    with pytest.raises(ReaderError, match=match):
        _reader([], calendar, protocol).read_window(symbol, start, end)


def test_window_bounds_are_query_parameters_not_sql_text(calendar: SessionCalendar, protocol: Any) -> None:
    client = FakeClient(_clean_monday())
    GovernedReader(client, calendar=calendar, protocol=protocol).read_window(SYMBOL, _ns(FRI, 14), _ns(MON, 14))

    sql, params = client.queries[-1]
    assert SYMBOL not in sql and str(_ns(FRI, 14)) not in sql
    utc_midnight = dt.datetime(2026, 3, 13, tzinfo=dt.UTC).timestamp() * 1_000_000_000
    assert params == {"symbol": SYMBOL, "start_ns": int(utc_midnight), "end_ns": int(utc_midnight) + 4 * 86_400 * 10**9}
    assert "ORDER BY exch_ts, ingest_ts, seq_no" in sql


def _doubled_bucket() -> list[tuple[Any, ...]]:
    """20 distinct trades in one 5-minute bucket, each delivered twice (multiplicity 2)."""
    rows: list[tuple[Any, ...]] = []
    for k in range(20):
        ts = _ns(MON, 9, 0, k)
        rows += [_tick(ts, BID + k * 1_000_000), _tick(ts, BID + k * 1_000_000)]
    return rows


def test_a_narrow_window_dedups_like_the_full_day_read(calendar: SessionCalendar, protocol: Any) -> None:
    reader = _reader(_doubled_bucket(), calendar, protocol)
    full = reader.read_trading_day(SYMBOL, MON)
    narrow = reader.read_window(SYMBOL, _ns(MON, 9, 0, 0), _ns(MON, 9, 0, 19))

    assert len(full.rows) == 20
    assert narrow.rows == tuple(r for r in full.rows if r[1] < _ns(MON, 9, 0, 19))
    assert len(narrow.rows) == 19
    assert narrow.rows_fetched == 38
    assert narrow.provenance()["dedup_context_ns"] == list(narrow.dedup_context)


def test_a_narrow_window_keeps_a_repeated_snapshot_out(calendar: SessionCalendar, protocol: Any) -> None:
    rows = [_bidask(_ns(MON, 9, 0, 0)), _bidask(_ns(MON, 9, 0, 0, 400_000))]
    reader = _reader(rows, calendar, protocol)
    full = reader.read_trading_day(SYMBOL, MON)
    narrow = reader.read_window(SYMBOL, _ns(MON, 9, 0, 0, 1), _ns(MON, 9, 1))

    assert len(full.rows) == 1
    assert narrow.rows == ()


def test_a_stale_quote_is_not_the_quote_in_force(calendar: SessionCalendar, protocol: Any) -> None:
    ts = _ns(MON, 9, 0)
    read = _reader([_bidask(ts)], calendar, protocol).read_trading_day(SYMBOL, MON)
    queries = np.array([ts + 1_000_000_000, ts + 10_000_000_000], dtype=np.int64)

    assert read.fresh_quote_index(queries).tolist() == [0, asof.NO_QUOTE]


def test_provenance_records_what_each_read_removed(calendar: SessionCalendar, protocol: Any) -> None:
    rows = _clean_monday() + [_tick(_ns(FRI, 14, 30))]
    payload = _reader(rows, calendar, protocol).read_trading_day(SYMBOL, MON).provenance()

    assert payload["reader"] == READER_ID
    assert payload["rows_fetched"] == 121 and payload["rows_out"] == 120
    assert payload["session_filtered_rows"] == 1
    assert payload["dedup_rule"] == dedup.RULE_ID
    assert payload["bidask_depth_counts"] == {"1": 30, "5": 30}


def test_trading_days_date_a_friday_night_to_monday(calendar: SessionCalendar, protocol: Any) -> None:
    night_only = [r for r in _clean_monday() if r[1] < _ns(SAT, 0)]

    assert _reader(night_only, calendar, protocol).trading_days(SYMBOL) == [MON]


def test_local_slots_take_exactly_one_of_root_or_symbol(calendar: SessionCalendar, protocol: Any) -> None:
    reader = _reader([], calendar, protocol)
    with pytest.raises(ReaderError, match="exactly one"):
        reader.local_slots(tz="Asia/Taipei", split_minute=840)
    with pytest.raises(ReaderError, match="exactly one"):
        reader.local_slots(tz="Asia/Taipei", split_minute=840, root="TMF", symbol=SYMBOL)


def test_connect_opens_no_client_until_the_first_query(monkeypatch: pytest.MonkeyPatch) -> None:
    import clickhouse_connect

    opened: list[dict[str, Any]] = []
    monkeypatch.setattr(clickhouse_connect, "get_client", lambda **kw: opened.append(kw) or FakeClient([]))
    reader = GovernedReader.connect(host="h", port=1, user="u", password="p")

    assert opened == []
    reader.ping()
    assert len(opened) == 1 and opened[0]["host"] == "h"


# --- consumer: MakerEngine ClickHouseSource -----------------------------------------------


def _source(rows: list[tuple[Any, ...]], calendar: SessionCalendar, protocol: Any) -> ClickHouseSource:
    return ClickHouseSource(reader=_reader(rows, calendar, protocol))


def test_clickhouse_source_matches_the_old_reader_on_a_clean_window(
    calendar: SessionCalendar, protocol: Any
) -> None:
    rows = _clean_monday()
    start, end = _ns(FRI, 14), _ns(MON, 14)

    assert _source(rows, calendar, protocol).load_window(SYMBOL, start, end) == _old_clickhouse_source_events(
        rows, start, end
    )


def test_clickhouse_source_load_day_is_the_trading_day_not_the_utc_date(
    calendar: SessionCalendar, protocol: Any
) -> None:
    events = _source(_clean_monday(), calendar, protocol).load_day(SYMBOL, MON.isoformat())

    # The old reader's UTC date 2026-03-16 started at 08:00 Taipei and missed Friday's night.
    assert events[0].exch_ts == _ns(FRI, 15, 0)
    assert len(events) == 120


def test_clickhouse_source_differs_from_the_old_reader_only_by_the_listed_rules(
    calendar: SessionCalendar, protocol: Any
) -> None:
    clean = _clean_monday()
    tie = _ns(MON, 10, 0)
    dirty = (
        [row for row in clean for _ in range(2 if row[0] == "Tick" else 1)]
        + [_tick(_ns(FRI, 14, 30))]
        # A trade that arrived before the quote stamped with the same exch_ts.
        + [_tick(tie, local=tie), _bidask(tie, local=tie + 1)]
    )
    start, end = _ns(FRI, 14), _ns(MON, 14)
    new = _source(dirty, calendar, protocol).load_window(SYMBOL, start, end)
    old = _old_clickhouse_source_events(dirty, start, end)

    assert len(old) - len(new) == 60 + 1  # duplicate deliveries + the out-of-session trade
    at_tie = [e.is_trade for e in new if e.exch_ts == tie]
    assert at_tie == [True, False]  # arrival order; the old reader put every quote first
    assert [e.is_trade for e in old if e.exch_ts == tie] == [False, True]


def test_clickhouse_source_keeps_the_provenance_of_every_read(calendar: SessionCalendar, protocol: Any) -> None:
    source = _source(_clean_monday(), calendar, protocol)
    source.load_day(SYMBOL, MON.isoformat())

    assert [r["reader"] for r in source.reads] == [READER_ID]


def test_clickhouse_source_available_dates_are_trading_days(calendar: SessionCalendar, protocol: Any) -> None:
    assert _source(_clean_monday(), calendar, protocol).available_dates(SYMBOL) == [MON.isoformat()]


def test_clickhouse_source_health_check_wraps_a_connection_failure() -> None:
    def _refuse() -> None:
        raise OSError("connection refused")

    source = ClickHouseSource(host="h", port=1, reader=GovernedReader(connect=_refuse))
    with pytest.raises(ConnectionError, match="ClickHouse not reachable"):
        source.health_check()


def test_maker_engine_no_longer_imports_requests() -> None:
    assert not hasattr(maker_engine, "requests")


# --- consumer: queue calibration (ChDataSource.load_day shape) -----------------------------


def test_hbt_source_assembles_the_same_events_as_chdatasource_on_a_clean_day(
    calendar: SessionCalendar, protocol: Any
) -> None:
    rows = _clean_monday()
    frame = pd.DataFrame(
        {
            "exch_ts": [r[1] for r in rows],
            "local_ts": [r[2] for r in rows],
            "event_type": [r[0] for r in rows],
            "price": [r[7] for r in rows],
            "volume": [r[8] for r in rows],
            "trade_direction": [r[9] for r in rows],
            "bid_prices": [r[3] for r in rows],
            "bid_volumes": [r[5] for r in rows],
            "ask_prices": [r[4] for r in rows],
            "ask_volumes": [r[6] for r in rows],
        }
    )
    expected = assemble_day_events(frame, price_scale=CK_PRICE_SCALE)
    source = GovernedHbtSource(_reader(rows, calendar, protocol))

    np.testing.assert_array_equal(source.load_day(SYMBOL, MON.isoformat()), expected)
    assert source.reads[0]["rows_out"] == 120


def test_hbt_source_refuses_a_day_with_no_in_session_rows(calendar: SessionCalendar, protocol: Any) -> None:
    from hft_platform.backtest.ch_data_source import DataValidationError

    source = GovernedHbtSource(_reader([_tick(_ns(FRI, 14, 30))], calendar, protocol))
    with pytest.raises(DataValidationError, match="no in-session rows"):
        source.load_day(SYMBOL, MON.isoformat())
