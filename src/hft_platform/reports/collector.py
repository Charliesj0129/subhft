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
from typing import TYPE_CHECKING

import structlog

from hft_platform.contracts.types import ScaledPrice
from hft_platform.monitor._types import CH_TO_PLATFORM_DIVISOR
from hft_platform.reports.models import (
    Bar5m,
    DepthBar,
    FlowBar,
    LargeTrade,
    SessionData,
)

if TYPE_CHECKING:
    from clickhouse_driver import Client

log = structlog.get_logger(__name__)

__all__ = [
    "_ch_to_platform",
    "_day_filter",
    "_night_filter",
    "DataCollector",
]

# Large-trade volume thresholds per symbol family
_LARGE_TRADE_THRESHOLDS: dict[str, int] = {
    "TXFD6": 10,
    "TMFD6": 30,
    "MXFD6": 30,
}
_DEFAULT_LARGE_TRADE_THRESHOLD = 10

# Memory cap for every CH query
_SETTINGS = "SETTINGS max_memory_usage = 2000000000"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _ch_to_platform(ch_price: int) -> int:
    """Convert a ClickHouse-scaled price (x1,000,000) to platform scale (x10,000)."""
    return ch_price // CH_TO_PLATFORM_DIVISOR


def _day_filter(date: str) -> str:
    """Return a SQL WHERE snippet for the day session (07:00–13:45 CST).

    Uses exch_ts range comparison with explicit Asia/Taipei timezone.
    Does NOT use toDate() to avoid UTC drift.
    """
    return (
        f"exch_ts >= toDateTime64('{date} 07:00:00', 3, 'Asia/Taipei') * 1000000000"
        f" AND exch_ts < toDateTime64('{date} 13:45:00', 3, 'Asia/Taipei') * 1000000000"
    )


def _night_filter(date: str) -> str:
    """Return a SQL WHERE snippet for the night session (15:00 CST → 05:00 next day).

    Night session starts at 15:00 CST on *date* and runs for 14 hours.
    Uses exch_ts range without toDate() to avoid UTC confusion.
    """
    start = f"toDateTime64('{date} 15:00:00', 3, 'Asia/Taipei') * 1000000000"
    end = (
        f"(toDateTime64('{date} 15:00:00', 3, 'Asia/Taipei')"
        f" + INTERVAL 14 HOUR) * 1000000000"
    )
    return f"exch_ts >= {start} AND exch_ts < {end}"


# ---------------------------------------------------------------------------
# DataCollector
# ---------------------------------------------------------------------------


class DataCollector:
    """Fetch and normalise market data from ClickHouse for one session."""

    def __init__(self, ch_host: str = "") -> None:
        from clickhouse_driver import Client  # guarded import

        host = ch_host or os.environ.get("HFT_CLICKHOUSE_HOST", "localhost")
        self._client: Client = Client(host=host)
        log.info("DataCollector initialised", ch_host=host)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def collect(
        self,
        session: str,
        date: str,
        symbol: str = "TXFD6",
    ) -> SessionData:
        """Collect all data for *symbol* on *date* for the given *session*.

        Parameters
        ----------
        session:
            ``"day"`` or ``"night"``.
        date:
            ISO date string, e.g. ``"2026-03-27"``.
        symbol:
            Instrument identifier, e.g. ``"TXFD6"``.
        """
        time_filter = _day_filter(date) if session == "day" else _night_filter(date)

        ohlcv = self._query_ohlcv(symbol, time_filter)
        bars = self._query_5m_bars(symbol, time_filter)
        flow = self._query_flow(symbol, time_filter)
        large = self._query_large_trades(symbol, time_filter)
        spread = self._query_spread_dist(symbol, time_filter)
        depth = self._query_depth_imbalance(symbol, time_filter)

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
                _ch_to_platform(argMin(price_scaled, exch_ts)) AS open,
                _ch_to_platform(argMax(price_scaled, exch_ts)) AS close,
                _ch_to_platform(min(price_scaled))             AS low,
                _ch_to_platform(max(price_scaled))             AS high,
                sum(volume)                                     AS volume,
                count()                                         AS tick_count
            FROM hft.market_data
            WHERE symbol = '{symbol}'
              AND type = 'Tick'
              AND {time_filter}
            {_SETTINGS}
        """
        # We perform the conversion in Python to avoid UDF dependency.
        sql = f"""
            SELECT
                argMin(price_scaled, exch_ts) AS open_ch,
                argMax(price_scaled, exch_ts) AS close_ch,
                min(price_scaled)             AS low_ch,
                max(price_scaled)             AS high_ch,
                sum(volume)                   AS volume,
                count()                       AS tick_count
            FROM hft.market_data
            WHERE symbol = '{symbol}'
              AND type = 'Tick'
              AND {time_filter}
            {_SETTINGS}
        """
        rows = self._client.execute(sql)
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
                    toDateTime64(exch_ts / 1e9, 3, 'Asia/Taipei')
                ))                                   AS ts,
                argMin(price_scaled, exch_ts)        AS open_ch,
                max(price_scaled)                    AS high_ch,
                min(price_scaled)                    AS low_ch,
                argMax(price_scaled, exch_ts)        AS close_ch,
                sum(volume)                          AS volume,
                count()                              AS ticks
            FROM hft.market_data
            WHERE symbol = '{symbol}'
              AND type = 'Tick'
              AND {time_filter}
            GROUP BY ts
            ORDER BY ts
            {_SETTINGS}
        """
        rows = self._client.execute(sql)
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
                WHERE symbol = '{symbol}'
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
        rows = self._client.execute(sql)
        result: list[FlowBar] = []
        for row in rows:
            ticks = int(row[1])
            up = int(row[3])
            dn = int(row[4])
            total = int(row[2])
            ud_ratio = (up - dn) / total if total > 0 else 0.0
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
                WHERE symbol = '{symbol}'
                  AND type = 'Tick'
                  AND volume >= {threshold}
                  AND {time_filter}
            )
            SELECT ts, price_scaled, volume, prev_price
            FROM ordered
            ORDER BY ts
            {_SETTINGS}
        """
        rows = self._client.execute(sql)
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
                toInt32((asks_price[1] - bids_price[1]) / 10000) AS spread_pts,
                count()                                            AS cnt
            FROM hft.market_data
            WHERE symbol = '{symbol}'
              AND type = 'BidAsk'
              AND length(bids_price) > 0
              AND length(asks_price) > 0
              AND {time_filter}
            GROUP BY spread_pts
            ORDER BY spread_pts
            {_SETTINGS}
        """
        rows = self._client.execute(sql)
        return {int(row[0]): int(row[1]) for row in rows}

    def _query_depth_imbalance(self, symbol: str, time_filter: str) -> list[DepthBar]:
        """Q6: Hourly average bid/ask depth at L1 and bid ratio."""
        sql = f"""
            SELECT
                toHour(toDateTime64(exch_ts / 1e9, 3, 'Asia/Taipei')) AS hour,
                avg(bids_vol[1])                                        AS avg_bid_vol,
                avg(asks_vol[1])                                        AS avg_ask_vol
            FROM hft.market_data
            WHERE symbol = '{symbol}'
              AND type = 'BidAsk'
              AND length(bids_vol) > 0
              AND length(asks_vol) > 0
              AND {time_filter}
            GROUP BY hour
            ORDER BY hour
            {_SETTINGS}
        """
        rows = self._client.execute(sql)
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
