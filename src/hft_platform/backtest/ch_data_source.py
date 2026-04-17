"""ClickHouse -> hftbacktest event_dtype streaming adapter.

Reads market data directly from ClickHouse and produces numpy arrays
conforming to hftbacktest's event_dtype specification.

Eliminates the .npz intermediate file and its associated export bugs
(notably the DEPTH_EVENT accumulation bug that caused 577x PnL overestimate).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import pandas as pd

import numpy as np

# hftbacktest event flags (from hftbacktest.types, replicated here as stable constants)
# https://github.com/nkaz001/hftbacktest/blob/master/py-hftbacktest/hftbacktest/types.py
DEPTH_EVENT = 1
TRADE_EVENT = 2
DEPTH_CLEAR_EVENT = 3
DEPTH_SNAPSHOT_EVENT = 4
EXCH_EVENT = 1 << 31
LOCAL_EVENT = 1 << 30
BUY_EVENT = 1 << 29
SELL_EVENT = 1 << 28

# Mask to extract the raw event-type integer from the composite ev field.
# Lower byte holds the event type (1=DEPTH, 2=TRADE, 3=DEPTH_CLEAR, 4=SNAPSHOT).
# Upper bits hold flags (EXCH_EVENT, LOCAL_EVENT, BUY_EVENT, SELL_EVENT).
EV_TYPE_MASK = 0xFF


class DataValidationError(RuntimeError):
    """Raised when loaded market data fails post-load sanity checks."""


@runtime_checkable
class BacktestDataSource(Protocol):
    """Protocol for backtest data sources."""

    def load_day(self, instrument: str, date: str) -> np.ndarray: ...

    def load_days(self, instrument: str, dates: list[str]) -> list[np.ndarray]: ...


class ChDataSource:
    """Streams ClickHouse market data as hftbacktest-compatible numpy arrays."""

    def __init__(
        self,
        ch_host: str = "localhost",
        ch_port: int = 8123,
        ch_user: str = "default",
        ch_password: str = "",
        price_scale: int = 1_000_000,
    ) -> None:
        """Initialize ChDataSource.

        Args:
            ch_host: ClickHouse host
            ch_port: ClickHouse HTTP port (default: 8123). ``clickhouse_connect``
                uses the HTTP interface — do NOT use the native TCP port 9000 here.
            ch_user: ClickHouse username (default: "default")
            ch_password: ClickHouse password; if empty, reads CLICKHOUSE_PASSWORD env var.
            price_scale: Scale factor for price descaling. ClickHouse / golden
                parquet stores prices at x1,000,000 scale (not the x10,000
                platform scale). Descaling happens at the boundary to produce
                float prices for hftbacktest.
        """
        import os  # noqa: PLC0415

        self.ch_host = ch_host
        self.ch_port = ch_port
        self.ch_user = ch_user
        self.ch_password = ch_password or os.getenv("CLICKHOUSE_PASSWORD", "")
        self.price_scale = price_scale

    def load_day(self, instrument: str, date: str, max_depth_levels: int = 5) -> np.ndarray:
        """Load one trading day as hftbacktest event_dtype array.

        Queries hft.market_data for the given instrument/date, converts rows
        into hftbacktest-compatible events, and validates the result.

        Real hft.market_data schema uses:
          - ``type`` (not event_type), ``ingest_ts`` (not local_ts),
          - ``price_scaled`` (not price), ``bids_price``/``bids_vol`` (not bid_prices/bid_volumes),
          - ``trade_direction`` Int8 (+1=buy, -1=sell, 0=no-direction, not side string)
        """
        # Lazy import to keep module load lightweight
        import clickhouse_connect  # noqa: PLC0415

        client = clickhouse_connect.get_client(
            host=self.ch_host,
            port=self.ch_port,
            username=self.ch_user,
            password=self.ch_password,
        )
        query = """
            SELECT
                exch_ts,
                ingest_ts AS local_ts,
                type AS event_type,
                price_scaled AS price,
                volume,
                trade_direction,
                bids_price AS bid_prices,
                bids_vol AS bid_volumes,
                asks_price AS ask_prices,
                asks_vol AS ask_volumes
            FROM hft.market_data
            WHERE symbol = {instrument:String}
              AND toDate(toDateTime64(exch_ts/1e9, 3)) = {date:Date}
            ORDER BY exch_ts
        """
        df = client.query_df(
            query, parameters={"instrument": instrument, "date": date},
        )
        if df.empty:
            raise DataValidationError(
                f"{instrument} {date}: no rows in hft.market_data"
            )

        events = assemble_day_events(df, price_scale=self.price_scale)
        validate_events(events, instrument=instrument)
        return events

    def load_days(self, instrument: str, dates: list[str]) -> list[np.ndarray]:
        return [self.load_day(instrument, d) for d in dates]


def _event_dtype() -> np.dtype:
    """hftbacktest event_dtype layout (8 fields, 64 bytes)."""
    return np.dtype([
        ("ev", "u8"),
        ("exch_ts", "i8"),
        ("local_ts", "i8"),
        ("px", "f8"),
        ("qty", "f8"),
        ("order_id", "u8"),
        ("ival", "i8"),
        ("fval", "f8"),
    ])


def build_bidask_events(
    exch_ts: int,
    local_ts: int,
    bid_prices: list[int],
    bid_volumes: list[int],
    ask_prices: list[int],
    ask_volumes: list[int],
    price_scale: int,
) -> np.ndarray:
    """Build hftbacktest events for one BidAsk snapshot.

    Emits DEPTH_CLEAR_EVENT first (snapshot semantics), then one DEPTH_EVENT
    per non-zero-volume price level on bid side, then ask side.
    Zero-volume levels are skipped.
    """
    dtype = _event_dtype()
    rows: list[tuple] = []

    # Clear event (wipes the depth state in hftbacktest)
    rows.append((
        DEPTH_CLEAR_EVENT | EXCH_EVENT | LOCAL_EVENT,
        exch_ts, local_ts, 0.0, 0.0, 0, 0, 0.0,
    ))

    for price, vol in zip(bid_prices, bid_volumes, strict=True):
        if vol <= 0 or price <= 0:
            continue
        rows.append((
            DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT,
            exch_ts, local_ts,
            price / price_scale, float(vol),
            0, 0, 0.0,
        ))

    for price, vol in zip(ask_prices, ask_volumes, strict=True):
        if vol <= 0 or price <= 0:
            continue
        rows.append((
            DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT,
            exch_ts, local_ts,
            price / price_scale, float(vol),
            0, 0, 0.0,
        ))

    return np.array(rows, dtype=dtype)


def build_tick_event(
    exch_ts: int, local_ts: int,
    price: int, volume: int, side: str,
    price_scale: int,
) -> np.ndarray:
    """Build one hftbacktest event for a trade tick."""
    dtype = _event_dtype()
    side_flag = BUY_EVENT if side == "Buy" else SELL_EVENT
    return np.array(
        [(
            TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT | side_flag,
            exch_ts, local_ts,
            price / price_scale, float(volume),
            0, 0, 0.0,
        )],
        dtype=dtype,
    )[0]


def assemble_day_events(df: "pd.DataFrame", price_scale: int) -> np.ndarray:
    """Convert one day of ClickHouse market_data rows into one hftbacktest event array.

    Accepts two column formats:

    **Real ClickHouse schema** (hft.market_data after SELECT aliasing):
      - ``event_type`` ("BidAsk" or "Tick"), ``local_ts``
      - BidAsk: ``bid_prices``, ``bid_volumes``, ``ask_prices``, ``ask_volumes`` (list[int]);
        ``price`` and ``volume`` are 0 for BidAsk rows and are ignored.
      - Tick: ``price`` (int), ``volume`` (int), ``trade_direction`` (Int8: +1=buy, -1=sell,
        0=no-direction). Rows with ``trade_direction == 0`` are skipped (no executable side).
        If the column is absent the legacy ``side`` string column is used instead.

    **Legacy / test format** (kept for backward compat with existing unit tests):
      - Tick rows may carry ``side`` ("Buy" / "Sell") in place of ``trade_direction``.

    Returns numpy structured array sorted by exch_ts.
    """
    dtype = _event_dtype()
    chunks: list[np.ndarray] = []

    df_sorted = df.sort_values("exch_ts", kind="stable").reset_index(drop=True)

    # Determine which side column is present
    has_trade_direction = "trade_direction" in df_sorted.columns
    has_side = "side" in df_sorted.columns

    for row in df_sorted.itertuples(index=False):
        if row.event_type == "BidAsk":
            # BidAsk rows in real schema have price_scaled=0, volume=0 — use arrays only.
            chunk = build_bidask_events(
                exch_ts=int(row.exch_ts), local_ts=int(row.local_ts),
                bid_prices=list(row.bid_prices), bid_volumes=list(row.bid_volumes),
                ask_prices=list(row.ask_prices), ask_volumes=list(row.ask_volumes),
                price_scale=price_scale,
            )
            chunks.append(chunk)
        elif row.event_type == "Tick":
            # Resolve side string from trade_direction (real schema) or side column (legacy).
            if has_trade_direction:
                direction = int(row.trade_direction)
                if direction > 0:
                    side_str = "Buy"
                elif direction < 0:
                    side_str = "Sell"
                else:
                    # trade_direction == 0: no confirmed direction; skip row.
                    continue
            elif has_side:
                side_str = str(row.side)
            else:
                # Cannot determine side; skip.
                continue

            event = build_tick_event(
                exch_ts=int(row.exch_ts), local_ts=int(row.local_ts),
                price=int(row.price), volume=int(row.volume), side=side_str,
                price_scale=price_scale,
            )
            chunks.append(np.array([event], dtype=dtype))

    if not chunks:
        return np.array([], dtype=dtype)
    return np.concatenate(chunks)


def _check_spread_sanity(events: np.ndarray, instrument: str) -> None:
    """Check spread sanity per snapshot to catch DEPTH_CLEAR accumulation bugs.

    Walks events; between each pair of DEPTH_CLEAR_EVENTs (or clear-to-end),
    verifies the reconstructed best_bid/best_ask are not inverted.
    Raises DataValidationError if best_ask <= best_bid in any snapshot.
    """
    current_best_bid = -1.0
    current_best_ask = float("inf")
    snapshot_had_bid = False
    snapshot_had_ask = False

    for i in range(len(events)):
        ev_flags = int(events[i]["ev"])
        ev_type = ev_flags & EV_TYPE_MASK

        if ev_type == DEPTH_CLEAR_EVENT:
            if snapshot_had_bid and snapshot_had_ask and current_best_ask <= current_best_bid:
                raise DataValidationError(
                    f"{instrument}: inverted book at row {i} "
                    f"(best_bid={current_best_bid}, best_ask={current_best_ask}) — "
                    "possible DEPTH_CLEAR accumulation bug"
                )
            current_best_bid = -1.0
            current_best_ask = float("inf")
            snapshot_had_bid = False
            snapshot_had_ask = False
        elif ev_type == DEPTH_EVENT:
            px = float(events[i]["px"])
            if px <= 0.0:
                continue
            if ev_flags & BUY_EVENT:
                if px > current_best_bid:
                    current_best_bid = px
                snapshot_had_bid = True
            elif ev_flags & SELL_EVENT:
                if px < current_best_ask:
                    current_best_ask = px
                snapshot_had_ask = True

    if snapshot_had_bid and snapshot_had_ask and current_best_ask <= current_best_bid:
        raise DataValidationError(
            f"{instrument}: inverted book in final snapshot "
            f"(best_bid={current_best_bid}, best_ask={current_best_ask})"
        )


def validate_events(events: np.ndarray, instrument: str) -> None:
    """Post-load validation. Raises DataValidationError with diagnostic details.

    Checks:
    1. Event array is non-empty
    2. At least one DEPTH_EVENT is present
    3. At least one TRADE_EVENT is present
    4. Timestamps (exch_ts) are monotonically non-decreasing
    5. No negative prices on non-clear events
    """
    if len(events) == 0:
        raise DataValidationError(f"{instrument}: empty event array")

    ev_types = events["ev"] & EV_TYPE_MASK
    has_depth = bool(np.any(ev_types == DEPTH_EVENT))
    has_trade = bool(np.any(ev_types == TRADE_EVENT))
    if not has_depth:
        raise DataValidationError(f"{instrument}: no depth events in array")
    if not has_trade:
        raise DataValidationError(f"{instrument}: no trade events in array")

    ts = events["exch_ts"]
    if len(ts) > 1 and np.any(ts[1:] < ts[:-1]):
        first_bad = int(np.argmax(ts[1:] < ts[:-1]))
        raise DataValidationError(
            f"{instrument}: timestamps not monotonic at row {first_bad}"
        )

    # Identify DEPTH_CLEAR rows to exclude from price check (clear events have px=0)
    is_clear = ev_types == DEPTH_CLEAR_EVENT
    non_clear = ~is_clear
    prices = events["px"][non_clear]
    nonzero_prices = prices[prices != 0.0]
    if len(nonzero_prices) and np.any(nonzero_prices < 0):
        raise DataValidationError(
            f"{instrument}: negative prices detected (min={float(nonzero_prices.min())})"
        )

    _check_spread_sanity(events, instrument)
