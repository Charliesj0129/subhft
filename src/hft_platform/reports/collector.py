"""DataCollector — fetches and normalises ClickHouse data for the Report Service.

Price scale notes
-----------------
- ClickHouse stores prices at x1,000,000 (CLICKHOUSE_PRICE_SCALE).
- Platform contracts use x10,000 (PLATFORM_SCALE).
- Conversion: ch_price // CH_TO_PLATFORM_DIVISOR  (divisor = 100).

The spread query groups by integer *points* so it divides by 10,000 in SQL
(one platform tick = 10,000 in CH; spread is already expressed in platform
units after the bids_price/asks_price subtraction which is in CH units, so
we divide by 10,000 to get integer points).
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable

import structlog

from hft_platform.contracts.types import ScaledPrice
from hft_platform.monitor._types import CH_TO_PLATFORM_DIVISOR
from hft_platform.reports.models import (
    Bar5m,
    DaySnapshot,
    DepthBar,
    FlowBar,
    LargeTrade,
    SessionData,
)

log = structlog.get_logger(__name__)

__all__ = [
    "_ch_to_platform",
    "_day_filter",
    "_night_filter",
    "_validate_time_filter",
    "DataCollector",
]

# Large-trade volume thresholds per symbol family
_LARGE_TRADE_THRESHOLDS: dict[str, int] = {
    "TXFD6": 10,
    "TMFD6": 30,
    "MXFD6": 30,
    "2330": 100,
}
_DEFAULT_LARGE_TRADE_THRESHOLD = 10

# Input validation patterns
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DANGEROUS_SQL_RE = re.compile(
    r"(;|--|\b(DROP|DELETE|INSERT|UPDATE|ALTER|GRANT|UNION|EXEC|CREATE|TRUNCATE)\b)",
    re.IGNORECASE,
)

# Memory cap for every CH query
_SETTINGS = "SETTINGS max_memory_usage = 2000000000"


def _validate_symbol(symbol: str) -> str:
    """Validate and return symbol, raising ValueError on bad input."""
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f"Invalid symbol: {symbol!r}")
    return symbol


def _validate_date(date: str) -> str:
    """Validate and return ISO date string, raising ValueError on bad input."""
    if not _DATE_RE.match(date):
        raise ValueError(f"Invalid date: {date!r}")
    return date


def _validate_time_filter(time_filter: str) -> str:
    """Validate a SQL WHERE snippet, raising ValueError on suspicious input.

    Uses a blocklist to reject tokens that could indicate SQL injection:
    semicolons, SQL comments (--), and dangerous DDL/DML keywords.
    """
    if _DANGEROUS_SQL_RE.search(time_filter):
        raise ValueError(f"Unsafe time_filter rejected: {time_filter!r}")
    return time_filter


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _ch_to_platform(ch_price: int) -> int:
    """Convert a ClickHouse-scaled price (x1,000,000) to platform scale (x10,000)."""
    return ch_price // CH_TO_PLATFORM_DIVISOR


_TS = "toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')"


def _day_filter(date: str) -> str:
    """Return a SQL WHERE snippet for the day session (07:00–13:45 CST).

    Uses toUnixTimestamp64Nano for efficient exch_ts range pre-filtering,
    then precise DateTime64 comparison for correctness.
    """
    lo = f"toUnixTimestamp64Nano(toDateTime64('{date} 06:00:00', 3, 'Asia/Taipei'))"
    hi = f"toUnixTimestamp64Nano(toDateTime64('{date} 14:00:00', 3, 'Asia/Taipei'))"
    return (
        f"exch_ts >= {lo} AND exch_ts < {hi} "
        f"AND {_TS} >= toDateTime64('{date} 07:00:00', 3, 'Asia/Taipei') "
        f"AND {_TS} < toDateTime64('{date} 13:45:00', 3, 'Asia/Taipei')"
    )


def _night_filter(date: str) -> str:
    """Return a SQL WHERE snippet for the night session (15:00 CST → 05:00 next day).

    Uses toUnixTimestamp64Nano for efficient exch_ts range pre-filtering.
    Night session: 15:00 CST on *date* for 14 hours.
    """
    lo = f"toUnixTimestamp64Nano(toDateTime64('{date} 14:00:00', 3, 'Asia/Taipei'))"
    hi = f"toUnixTimestamp64Nano(toDateTime64('{date} 15:00:00', 3, 'Asia/Taipei') + INTERVAL 15 HOUR)"
    return (
        f"exch_ts >= {lo} AND exch_ts < {hi} "
        f"AND {_TS} >= toDateTime64('{date} 15:00:00', 3, 'Asia/Taipei') "
        f"AND {_TS} < toDateTime64('{date} 15:00:00', 3, 'Asia/Taipei') + INTERVAL 14 HOUR"
    )


# ---------------------------------------------------------------------------
# ClickHouse client abstraction
# ---------------------------------------------------------------------------

ExecuteFn = Callable[[str, "dict[str, Any] | None"], list[tuple[Any, ...]]]


def _make_execute(host: str) -> ExecuteFn:
    """Return a ``(sql, params) -> list[tuple]`` callable using whichever CH client is installed."""
    user = os.environ.get("HFT_CLICKHOUSE_USER", os.environ.get("CLICKHOUSE_USER", "default"))
    password = os.environ.get("HFT_CLICKHOUSE_PASSWORD", os.environ.get("CLICKHOUSE_PASSWORD", ""))

    try:
        import clickhouse_connect

        kwargs: dict[str, Any] = {"host": host, "username": user}
        if password:
            kwargs["password"] = password
        client = clickhouse_connect.get_client(**kwargs)
        log.info("DataCollector using clickhouse_connect", host=host)

        def _exec(sql: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
            if params:
                import re as _re

                cc_sql = _re.sub(r"%\((\w+)\)s", r"{\1:String}", sql)
                return client.query(cc_sql, parameters=params).result_rows  # type: ignore[return-value]
            return client.query(sql).result_rows  # type: ignore[return-value]

        return _exec
    except ImportError:
        pass

    from clickhouse_driver import Client  # type: ignore[import-untyped]

    client_native = Client(host=host, user=user, password=password)
    log.info("DataCollector using clickhouse_driver", host=host)

    def _exec_native(sql: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
        return client_native.execute(sql, params)

    return _exec_native


# ---------------------------------------------------------------------------
# DataCollector
# ---------------------------------------------------------------------------


class DataCollector:
    """Fetch and normalise market data from ClickHouse for one session.

    Supports both ``clickhouse-connect`` (production Docker image) and
    ``clickhouse-driver`` (dev) transparently.
    """

    def __init__(self, ch_host: str = "") -> None:
        host = ch_host or os.environ.get("HFT_CLICKHOUSE_HOST", "localhost")
        self._execute = _make_execute(host)
        log.info("DataCollector initialised", ch_host=host)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def collect_core(
        self,
        symbol: str,
        time_filter: str,
        *,
        session: str = "",
        date: str = "",
    ) -> SessionData:
        """Collect lightweight data for *symbol* using a pre-built *time_filter*.

        Runs only Q1 (OHLCV), Q2 (5m bars), Q3 (flow), Q4 (large trades).
        Q5 (spread distribution) and Q6 (depth imbalance) are skipped;
        the returned :class:`SessionData` will have ``spread_dist={}`` and
        ``depth_imbalance=[]``.

        This is the preferred entry point for commands that do not require the
        heavy Array-column queries (e.g. the Telegram bot ``/levels`` and
        ``/flow`` commands).

        Parameters
        ----------
        symbol:
            Instrument identifier, e.g. ``"TXFD6"``.
        time_filter:
            A SQL WHERE snippet for the desired time range (produced by
            :func:`_day_filter` or :func:`_night_filter`).
        session:
            Optional session label (``"day"`` / ``"night"``) stored on the
            result; defaults to ``""``.
        date:
            Optional ISO date string stored on the result; defaults to ``""``.
        """
        _validate_symbol(symbol)
        _validate_time_filter(time_filter)
        ohlcv = self._query_ohlcv(symbol, time_filter)
        bars = self._query_5m_bars(symbol, time_filter)
        flow = self._query_flow(symbol, time_filter)
        large = self._query_large_trades(symbol, time_filter)

        return SessionData(
            session=session,
            symbol=symbol,
            date=date,
            open=ScaledPrice(ohlcv["open"]),
            high=ScaledPrice(ohlcv["high"]),
            low=ScaledPrice(ohlcv["low"]),
            close=ScaledPrice(ohlcv["close"]),
            volume=ohlcv["volume"],
            tick_count=ohlcv["tick_count"],
            bars_5m=bars,
            flow_5m=flow,
            large_trades=large,
            spread_dist={},
            depth_imbalance=[],
        )

    def collect(
        self,
        session: str,
        date: str,
        symbol: str = "TXFD6",
    ) -> SessionData:
        """Collect all data for *symbol* on *date* for the given *session*.

        Delegates Q1–Q4 to :meth:`collect_core`, then appends Q5 (spread
        distribution) and Q6 (depth imbalance) with graceful OOM degradation.

        Parameters
        ----------
        session:
            ``"day"`` or ``"night"``.
        date:
            ISO date string, e.g. ``"2026-03-27"``.
        symbol:
            Instrument identifier, e.g. ``"TXFD6"``.
        """
        _validate_symbol(symbol)
        _validate_date(date)
        time_filter = _day_filter(date) if session == "day" else _night_filter(date)

        sd = self.collect_core(symbol, time_filter, session=session, date=date)

        # Q5 and Q6 — heavy Array-column queries; gracefully degrade on OOM.
        try:
            spread = self._query_spread_dist(symbol, time_filter)
        except Exception:  # noqa: BLE001
            log.warning("Q5 spread query failed (likely OOM), skipping", symbol=symbol)
            spread = {}
        try:
            depth = self._query_depth_imbalance(symbol, time_filter)
        except Exception:  # noqa: BLE001
            log.warning("Q6 depth query failed (likely OOM), skipping", symbol=symbol)
            depth = []

        # SessionData uses slots=True — cannot mutate; create a new instance.
        return SessionData(
            session=sd.session,
            symbol=sd.symbol,
            date=sd.date,
            open=sd.open,
            high=sd.high,
            low=sd.low,
            close=sd.close,
            volume=sd.volume,
            tick_count=sd.tick_count,
            bars_5m=sd.bars_5m,
            flow_5m=sd.flow_5m,
            large_trades=sd.large_trades,
            spread_dist=spread,
            depth_imbalance=depth,
        )

    # ------------------------------------------------------------------
    # Internal queries
    # ------------------------------------------------------------------

    def _query_ohlcv(self, symbol: str, time_filter: str) -> dict:
        """Q1: Session OHLCV."""
        sql = f"""
            SELECT
                argMin(open_scaled, bucket)   AS open_ch,
                argMax(close_scaled, bucket)  AS close_ch,
                min(low_scaled)               AS low_ch,
                max(high_scaled)              AS high_ch,
                sum(volume)                   AS volume,
                sum(tick_count)               AS tick_count
            FROM hft.ohlcv_1m
            WHERE symbol = %(symbol)s
              AND {time_filter}
            {_SETTINGS}
        """
        sql = sql.replace(
            "FROM hft.ohlcv_1m",
            """FROM (
                SELECT
                    symbol,
                    bucket,
                    open_scaled,
                    high_scaled,
                    low_scaled,
                    close_scaled,
                    volume,
                    tick_count,
                    toUnixTimestamp64Nano(toDateTime64(bucket, 3, 'Asia/Taipei')) AS exch_ts
                FROM hft.ohlcv_1m
            ) AS ohlcv_1m""",
        )
        params = {"symbol": symbol}
        rows = self._execute(sql, params)
        if not rows or not rows[0][0]:
            log.warning("Q1 OHLCV returned no data", symbol=symbol)
            return {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "tick_count": 0}
        row = rows[0]
        return {
            "open": _ch_to_platform(int(row[0])),
            "close": _ch_to_platform(int(row[1])),
            "low": _ch_to_platform(int(row[2])),
            "high": _ch_to_platform(int(row[3])),
            "volume": int(row[4]),
            "tick_count": int(row[5]),
        }

    def _query_5m_bars(self, symbol: str, time_filter: str) -> list[Bar5m]:
        """Q2: 5-minute OHLCV bars."""
        sql = f"""
            SELECT
                toString(toStartOfFiveMinutes(
                    bucket
                ))                                   AS ts,
                argMin(open_scaled, bucket)          AS open_ch,
                max(high_scaled)                     AS high_ch,
                min(low_scaled)                      AS low_ch,
                argMax(close_scaled, bucket)         AS close_ch,
                sum(volume)                          AS volume,
                sum(tick_count)                      AS ticks
            FROM hft.ohlcv_1m
            WHERE symbol = %(symbol)s
              AND {time_filter}
            GROUP BY ts
            ORDER BY ts
            {_SETTINGS}
        """
        sql = sql.replace(
            "FROM hft.ohlcv_1m",
            """FROM (
                SELECT
                    symbol,
                    bucket,
                    open_scaled,
                    high_scaled,
                    low_scaled,
                    close_scaled,
                    volume,
                    tick_count,
                    toUnixTimestamp64Nano(toDateTime64(bucket, 3, 'Asia/Taipei')) AS exch_ts
                FROM hft.ohlcv_1m
            ) AS ohlcv_1m""",
        )
        params = {"symbol": symbol}
        rows = self._execute(sql, params)
        return [
            Bar5m(
                ts=str(row[0]),
                open=ScaledPrice(_ch_to_platform(int(row[1]))),
                high=ScaledPrice(_ch_to_platform(int(row[2]))),
                low=ScaledPrice(_ch_to_platform(int(row[3]))),
                close=ScaledPrice(_ch_to_platform(int(row[4]))),
                volume=int(row[5]),
                ticks=int(row[6]),
            )
            for row in rows
        ]

    def _query_flow(self, symbol: str, time_filter: str) -> list[FlowBar]:
        """Q3: Uptick / downtick flow per 5-minute bucket using lagInFrame."""
        sql = f"""
            WITH tick_data AS (
                SELECT
                    toStartOfFiveMinutes(
                        toDateTime64(exch_ts / 1e9, 3, 'Asia/Taipei')
                    )                                                   AS bucket,
                    price_scaled,
                    volume,
                    lagInFrame(price_scaled) OVER (
                        ORDER BY exch_ts
                        ROWS BETWEEN 1 PRECEDING AND CURRENT ROW
                    ) AS prev_price
                FROM hft.market_data
                WHERE symbol = %(symbol)s
                  AND type = 'Tick'
                  AND {time_filter}
            )
            SELECT
                toString(bucket)                        AS ts,
                count()                                 AS ticks,
                sum(volume)                             AS total_vol,
                sumIf(volume, price_scaled > prev_price) AS uptick_vol,
                sumIf(volume, price_scaled < prev_price) AS downtick_vol,
                sumIf(volume, price_scaled = prev_price) AS flat_vol
            FROM tick_data
            GROUP BY bucket
            ORDER BY bucket
            {_SETTINGS}
        """
        params = {"symbol": symbol}
        rows = self._execute(sql, params)
        result: list[FlowBar] = []
        for row in rows:
            ticks = int(row[1])
            up = int(row[3])
            dn = int(row[4])
            total = int(row[2])
            ud_ratio = up / dn if dn > 0 else (float(up) if up > 0 else 1.0)
            result.append(
                FlowBar(
                    ts=str(row[0]),
                    ticks=ticks,
                    total_vol=total,
                    uptick_vol=up,
                    downtick_vol=dn,
                    flat_vol=int(row[5]),
                    ud_ratio=ud_ratio,
                    net_flow=up - dn,
                )
            )
        return result

    def _query_large_trades(self, symbol: str, time_filter: str) -> list[LargeTrade]:
        """Q4: Trades at or above the large-trade volume threshold."""
        threshold = _LARGE_TRADE_THRESHOLDS.get(symbol, _DEFAULT_LARGE_TRADE_THRESHOLD)
        sql = f"""
            WITH ordered AS (
                SELECT
                    toString(toDateTime64(exch_ts / 1e9, 3, 'Asia/Taipei')) AS ts,
                    price_scaled,
                    volume,
                    lagInFrame(price_scaled) OVER (ORDER BY exch_ts
                        ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS prev_price
                FROM hft.market_data
                WHERE symbol = %(symbol)s
                  AND type = 'Tick'
                  AND volume >= {threshold}
                  AND {time_filter}
            )
            SELECT ts, price_scaled, volume, prev_price
            FROM ordered
            ORDER BY ts
            {_SETTINGS}
        """
        params = {"symbol": symbol}
        rows = self._execute(sql, params)
        result: list[LargeTrade] = []
        for row in rows:
            price_ch = int(row[1])
            prev_ch = int(row[3]) if row[3] else price_ch
            if price_ch > prev_ch:
                direction = "buy"
            elif price_ch < prev_ch:
                direction = "sell"
            else:
                direction = "unknown"
            result.append(
                LargeTrade(
                    ts=str(row[0]),
                    price=ScaledPrice(_ch_to_platform(price_ch)),
                    volume=int(row[2]),
                    direction=direction,
                )
            )
        return result

    def _query_spread_dist(self, symbol: str, time_filter: str) -> dict[int, int]:
        """Q5: Spread distribution in integer points.

        Spread = asks_price[1] - bids_price[1] (both in CH units x1,000,000).
        Divide by 10,000 to convert to integer platform ticks (1 point).
        """
        sql = f"""
            SELECT
                toInt32((asks_price[1] - bids_price[1]) / 1000000) AS spread_pts,
                count()                                            AS cnt
            FROM hft.market_data
            WHERE symbol = %(symbol)s
              AND type = 'BidAsk'
              AND length(bids_price) > 0
              AND length(asks_price) > 0
              AND asks_price[1] > bids_price[1]
              AND {time_filter}
            GROUP BY spread_pts
            ORDER BY spread_pts
            SETTINGS max_memory_usage = 3000000000
        """
        params = {"symbol": symbol}
        rows = self._execute(sql, params)
        return {int(row[0]): int(row[1]) for row in rows}

    def _query_depth_imbalance(self, symbol: str, time_filter: str) -> list[DepthBar]:
        """Q6: Hourly average bid/ask depth at L1 and bid ratio."""
        sql = f"""
            SELECT
                toHour(toDateTime64(exch_ts / 1e9, 3, 'Asia/Taipei')) AS hour,
                avg(bids_vol[1])                                        AS avg_bid_vol,
                avg(asks_vol[1])                                        AS avg_ask_vol
            FROM hft.market_data
            WHERE symbol = %(symbol)s
              AND type = 'BidAsk'
              AND length(bids_vol) > 0
              AND length(asks_vol) > 0
              AND {time_filter}
            GROUP BY hour
            ORDER BY hour
            {_SETTINGS}
        """
        params = {"symbol": symbol}
        rows = self._execute(sql, params)
        result: list[DepthBar] = []
        for row in rows:
            avg_bid = float(row[1])
            avg_ask = float(row[2])
            total = avg_bid + avg_ask
            bid_ratio = avg_bid / total if total > 0 else 0.5
            result.append(
                DepthBar(
                    hour=int(row[0]),
                    avg_bid_vol=avg_bid,
                    avg_ask_vol=avg_ask,
                    bid_ratio=bid_ratio,
                )
            )
        return result

    # ------------------------------------------------------------------
    # Q7: Cross-day OHLCV + flow
    # ------------------------------------------------------------------

    def collect_cross_day(
        self,
        symbol: str,
        session: str,
        date: str,
        lookback_days: int = 3,
    ) -> list[DaySnapshot]:
        """Q7: Fetch OHLCV + uptick/downtick for previous N trading days.

        Returns list of :class:`DaySnapshot` sorted by date descending
        (most recent first).  Weekends (Saturday/Sunday) are skipped.

        Parameters
        ----------
        symbol:
            Instrument identifier, e.g. ``"TXFD6"``.
        session:
            ``"day"`` or ``"night"``.
        date:
            Reference ISO date string (not included in results).
        lookback_days:
            Number of previous trading days to fetch (default 3).
        """
        _validate_symbol(symbol)
        _validate_date(date)

        from datetime import date as _date
        from datetime import timedelta

        ref = _date.fromisoformat(date)
        prev_dates: list[str] = []
        cursor = ref - timedelta(days=1)
        while len(prev_dates) < lookback_days:
            # Skip weekends: 5 = Saturday, 6 = Sunday
            if cursor.weekday() < 5:
                prev_dates.append(cursor.isoformat())
            cursor -= timedelta(days=1)
            # Safety: don't look back more than 30 calendar days
            if (ref - cursor).days > 30:
                break

        if not prev_dates:
            return []

        # Build a single UNION ALL query for all dates
        filter_fn = _day_filter if session == "day" else _night_filter
        parts: list[str] = []
        for d in prev_dates:
            tf = filter_fn(d)
            part = f"""
                SELECT
                    '{d}'                                     AS day,
                    argMin(price_scaled, exch_ts)             AS open_ch,
                    max(price_scaled)                         AS high_ch,
                    min(price_scaled)                         AS low_ch,
                    argMax(price_scaled, exch_ts)             AS close_ch,
                    sum(volume)                               AS total_vol,
                    sumIf(volume, price_scaled > lagInFrame(price_scaled) OVER (
                        ORDER BY exch_ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW
                    ))                                        AS uptick_vol,
                    sumIf(volume, price_scaled < lagInFrame(price_scaled) OVER (
                        ORDER BY exch_ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW
                    ))                                        AS downtick_vol
                FROM hft.market_data
                WHERE symbol = %(symbol)s
                  AND type = 'Tick'
                  AND {tf}
            """
            parts.append(part)

        sql = " UNION ALL ".join(parts) + f" ORDER BY day DESC {_SETTINGS}"
        params = {"symbol": symbol}

        try:
            rows = self._execute(sql, params)
        except Exception:  # noqa: BLE001
            log.warning("Q7 cross-day query failed", symbol=symbol)
            return []

        snapshots: list[DaySnapshot] = []
        for row in rows:
            day_str = str(row[0])
            total_vol = int(row[5])
            if total_vol == 0:
                continue
            up = int(row[6])
            dn = int(row[7])
            ud_ratio = up / dn if dn > 0 else 99.0
            net_flow = up - dn
            snapshots.append(
                DaySnapshot(
                    date=day_str,
                    session=session,
                    open=_ch_to_platform(int(row[1])),
                    high=_ch_to_platform(int(row[2])),
                    low=_ch_to_platform(int(row[3])),
                    close=_ch_to_platform(int(row[4])),
                    volume=total_vol,
                    ud_ratio=ud_ratio,
                    net_flow=net_flow,
                )
            )
        return snapshots
