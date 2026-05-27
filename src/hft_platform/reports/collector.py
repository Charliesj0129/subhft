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
import time
from typing import Any, Callable, TypeVar

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
    "_is_transient",
    "_night_filter",
    "_validate_time_filter",
    "_with_retry",
    "DataCollector",
]

# Large-trade volume thresholds per symbol root (month-code agnostic).
# Lookup: try exact match first, then strip trailing 2-char month code.
_LARGE_TRADE_THRESHOLDS: dict[str, int] = {
    "TXF": 10,
    "TMF": 30,
    "MXF": 30,
    "2330": 100,
}
_DEFAULT_LARGE_TRADE_THRESHOLD = 10


def _get_large_trade_threshold(symbol: str) -> int:
    """Resolve large-trade threshold with root-prefix fallback."""
    threshold = _LARGE_TRADE_THRESHOLDS.get(symbol)
    if threshold is not None:
        return threshold
    # Root-prefix fallback: strip month code (e.g. TMFE6 → TMF)
    if len(symbol) >= 5:
        root = symbol[:-2]
        threshold = _LARGE_TRADE_THRESHOLDS.get(root)
        if threshold is not None:
            return threshold
    return _DEFAULT_LARGE_TRADE_THRESHOLD


# Input validation patterns
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DANGEROUS_SQL_RE = re.compile(
    r"(;|--|\b(DROP|DELETE|INSERT|UPDATE|ALTER|GRANT|UNION|EXEC|CREATE|TRUNCATE)\b)",
    re.IGNORECASE,
)

# Continuous-contract alias suffixes (R1/R2/C0/C1) — see contracts/ref.py.
# Aliases like "TXFR1" never appear in hft.market_data (which stores the
# resolved month code, e.g. "TXFE6"). We must resolve at query time or every
# WHERE symbol = 'TXFR1' returns 0 rows. The live-broker resolver
# (ContractsRuntime.resolve_symbol_aliases) is unavailable here because the
# report pipeline runs out-of-band; we instead derive the active month code
# from CH itself by picking the highest-volume root match in recent data.
_ALIAS_SUFFIX_RE = re.compile(r"^([A-Z]{2,4})(R[12]|C[01])$")
# Lookback window for resolving an alias to its active month code. Long enough
# to cover the longest gap a reporter cares about (single-week run) but bounded
# so that a recently-rolled-over front month wins over the prior expiring one.
_ALIAS_RESOLUTION_LOOKBACK_DAYS = 14

# Memory cap for every CH query
_SETTINGS = "SETTINGS max_memory_usage = 2000000000"

# ClickHouse connection / query timeouts and retry config
_CH_CONNECT_TIMEOUT = 10
_CH_QUERY_TIMEOUT = 60
_CH_RETRY_DELAY_S = 2.0

T = TypeVar("T")


def _is_transient(exc: Exception) -> bool:
    """Return True for transient ClickHouse errors worth retrying."""
    exc_type = type(exc).__name__
    exc_msg = str(exc).lower()
    return any(
        keyword in exc_type.lower() or keyword in exc_msg
        for keyword in ("timeout", "connection", "refused", "reset", "broken pipe", "eof")
    )


def _with_retry(fn: Callable[[], T]) -> T:
    """Call *fn* with one retry on transient errors, backing off by ``_CH_RETRY_DELAY_S``."""
    for attempt in range(2):
        try:
            return fn()
        except Exception as exc:
            if attempt == 0 and _is_transient(exc):
                log.warning("ch_query_transient_error", attempt=attempt, error=str(exc))
                time.sleep(_CH_RETRY_DELAY_S)
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover — satisfies type checker


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

        kwargs: dict[str, Any] = {
            "host": host,
            "username": user,
            "connect_timeout": _CH_CONNECT_TIMEOUT,
            "send_receive_timeout": _CH_QUERY_TIMEOUT,
        }
        if password:
            kwargs["password"] = password
        client = clickhouse_connect.get_client(**kwargs)
        log.info("DataCollector using clickhouse_connect", host=host)

        def _exec(sql: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
            def _do() -> list[tuple[Any, ...]]:
                if params:
                    import re as _re

                    cc_sql = _re.sub(r"%\((\w+)\)s", r"{\1:String}", sql)
                    return client.query(cc_sql, parameters=params).result_rows  # type: ignore[return-value]
                return client.query(sql).result_rows  # type: ignore[return-value]

            return _with_retry(_do)

        return _exec
    except ImportError:
        pass

    from clickhouse_driver import Client  # type: ignore[import-untyped]

    client_native = Client(
        host=host,
        user=user,
        password=password,
        connect_timeout=_CH_CONNECT_TIMEOUT,
        send_receive_timeout=_CH_QUERY_TIMEOUT,
    )
    log.info("DataCollector using clickhouse_driver", host=host)

    def _exec_native(sql: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
        def _do() -> list[tuple[Any, ...]]:
            return client_native.execute(sql, params)

        return _with_retry(_do)

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
        # Cache resolved alias→month-code for the lifetime of this collector
        # so each report run pays at most one extra CH query per family.
        self._alias_cache: dict[str, str] = {}
        log.info("DataCollector initialised", ch_host=host)

    # ------------------------------------------------------------------
    # Alias resolution
    # ------------------------------------------------------------------

    def _resolve_alias(self, symbol: str) -> str:
        """Resolve continuous-contract aliases (TXFR1) to month codes (TXFE6).

        The platform stores resolved month codes in ``hft.market_data``;
        passing a raw alias yields zero rows. We derive the active month code
        by inspecting recent volume in CH for the alias's root prefix and
        pick the heaviest. Non-aliases (stocks, options, already-resolved
        month codes) are returned unchanged. Cached per-collector.

        Idempotent: ``_resolve_alias("TXFE6")`` returns ``"TXFE6"`` unchanged
        because the regex does not match resolved month codes.
        """
        if not symbol:
            return symbol
        cached = self._alias_cache.get(symbol)
        if cached is not None:
            return cached
        match = _ALIAS_SUFFIX_RE.match(symbol)
        if not match:
            # Not an alias form — pass through. Cache to skip repeat regex.
            self._alias_cache[symbol] = symbol
            return symbol
        root = match.group(1)
        # Pick the highest-volume month code matching ``<root>[A-L][0-9]`` *on
        # the most recent trading day that has data*, within the lookback window.
        #
        # Why not rank by trailing-window cumulative volume: on a monthly roll
        # the just-expired contract keeps the largest 14-day cumulative volume
        # for ~2 weeks, so a cumulative ranking resolves TXFR1 to the dead
        # contract (zero ticks post-roll) and every report returns no_data until
        # the new front month overtakes it. Scoping the ranking to the latest
        # day with data makes the live front month win on the roll date itself.
        # (Regression: 2026-05-21 daily-report blackout after TXFE6 expiry.)
        #
        # The match() regex is anchored so injection via the symbol parameter
        # cannot escape; root has already been validated by ``_validate_symbol``.
        sql = f"""
            SELECT symbol, sum(volume) AS vol
            FROM hft.market_data
            WHERE match(symbol, '^{root}[A-L][0-9]$')
              AND exch_ts >= toUnixTimestamp64Nano(now64(3) - INTERVAL {_ALIAS_RESOLUTION_LOOKBACK_DAYS} DAY)
              AND toDate(exch_ts / 1e9) = (
                  SELECT max(toDate(exch_ts / 1e9))
                  FROM hft.market_data
                  WHERE match(symbol, '^{root}[A-L][0-9]$')
                    AND exch_ts >= toUnixTimestamp64Nano(now64(3) - INTERVAL {_ALIAS_RESOLUTION_LOOKBACK_DAYS} DAY)
              )
            GROUP BY symbol
            ORDER BY vol DESC
            LIMIT 1
            {_SETTINGS}
        """  # nosec B608
        try:
            rows = self._execute(sql, None)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "alias_resolution_failed",
                symbol=symbol,
                root=root,
                error=str(exc),
            )
            self._alias_cache[symbol] = symbol
            return symbol
        if not rows or not rows[0] or not rows[0][0]:
            log.warning("alias_resolution_no_match", symbol=symbol, root=root)
            self._alias_cache[symbol] = symbol
            return symbol
        resolved = str(rows[0][0])
        log.debug(
            "alias_resolved",
            alias=symbol,
            resolved=resolved,
            volume=int(rows[0][1]) if rows[0][1] is not None else 0,
        )
        self._alias_cache[symbol] = resolved
        return resolved

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
        symbol = self._resolve_alias(symbol)
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
        """  # nosec B608
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
        symbol = self._resolve_alias(symbol)
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
        """  # nosec B608
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
        symbol = self._resolve_alias(symbol)
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
        """  # nosec B608
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
        symbol = self._resolve_alias(symbol)
        threshold = _get_large_trade_threshold(symbol)
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
        """  # nosec B608
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
        symbol = self._resolve_alias(symbol)
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
        """  # nosec B608
        params = {"symbol": symbol}
        rows = self._execute(sql, params)
        return {int(row[0]): int(row[1]) for row in rows}

    def _query_depth_imbalance(self, symbol: str, time_filter: str) -> list[DepthBar]:
        """Q6: Hourly average bid/ask depth at L1 and bid ratio."""
        symbol = self._resolve_alias(symbol)
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
        """  # nosec B608
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
        symbol = self._resolve_alias(symbol)

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
            # The lag must be computed in a subquery (ClickHouse rejects a
            # window function nested inside an aggregate argument, Code 184
            # ILLEGAL_AGGREGATION) — mirrors the Q3 _query_flow pattern.
            part = f"""
                SELECT
                    '{d}'                                     AS day,
                    argMin(price_scaled, exch_ts)             AS open_ch,
                    max(price_scaled)                         AS high_ch,
                    min(price_scaled)                         AS low_ch,
                    argMax(price_scaled, exch_ts)             AS close_ch,
                    sum(volume)                               AS total_vol,
                    sumIf(volume, price_scaled > prev_price)  AS uptick_vol,
                    sumIf(volume, price_scaled < prev_price)  AS downtick_vol
                FROM (
                    SELECT
                        exch_ts,
                        price_scaled,
                        volume,
                        lagInFrame(price_scaled) OVER (
                            ORDER BY exch_ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW
                        ) AS prev_price
                    FROM hft.market_data
                    WHERE symbol = %(symbol)s
                      AND type = 'Tick'
                      AND {tf}
                )
            """  # nosec B608
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
