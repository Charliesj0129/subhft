"""ClickHouse -> hftbacktest event_dtype streaming adapter.

Reads market data directly from ClickHouse and produces numpy arrays
conforming to hftbacktest's event_dtype specification.

Eliminates the .npz intermediate file and its associated export bugs
(notably the DEPTH_EVENT accumulation bug that caused 577x PnL overestimate).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

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
        ch_port: int = 9000,
        price_scale: int = 1_000_000,
    ) -> None:
        """Initialize ChDataSource.

        Args:
            ch_host: ClickHouse host
            ch_port: ClickHouse native protocol port
            price_scale: Scale factor for price descaling. ClickHouse / golden
                parquet stores prices at x1,000,000 scale (not the x10,000
                platform scale). Descaling happens at the boundary to produce
                float prices for hftbacktest.
        """
        self.ch_host = ch_host
        self.ch_port = ch_port
        self.price_scale = price_scale

    def load_day(self, instrument: str, date: str, max_depth_levels: int = 5) -> np.ndarray:
        raise NotImplementedError

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
