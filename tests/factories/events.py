"""Factories for event objects used across the test suite.

All prices use scaled integers (x10000) per the Precision Law.
Timestamps use ``timebase.now_ns()`` per project convention.
"""

from __future__ import annotations

from typing import Union

import numpy as np

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.core import timebase
from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData, TickEvent


def _meta(
    *,
    seq: int = 1,
    source_ts: int = 0,
    local_ts: int = 0,
    topic: str = "",
) -> MetaData:
    """Build a ``MetaData`` with sensible defaults."""
    now = timebase.now_ns()
    return MetaData(
        seq=seq,
        source_ts=source_ts or now,
        local_ts=local_ts or now,
        topic=topic,
    )


def make_tick_event(
    *,
    symbol: str = "2330",
    price: int = 5_000_000,
    volume: int = 100,
    total_volume: int = 0,
    bid_side_total_vol: int = 0,
    ask_side_total_vol: int = 0,
    is_simtrade: bool = False,
    is_odd_lot: bool = False,
    seq: int = 1,
    source_ts: int = 0,
    local_ts: int = 0,
    topic: str = "",
) -> TickEvent:
    """Create a ``TickEvent`` with sensible defaults.

    ``price`` is scaled x10000 (default 5_000_000 = 500.0 TWD).
    """
    return TickEvent(
        meta=_meta(seq=seq, source_ts=source_ts, local_ts=local_ts, topic=topic),
        symbol=symbol,
        price=price,
        volume=volume,
        total_volume=total_volume,
        bid_side_total_vol=bid_side_total_vol,
        ask_side_total_vol=ask_side_total_vol,
        is_simtrade=is_simtrade,
        is_odd_lot=is_odd_lot,
    )


def make_bidask_event(
    *,
    symbol: str = "2330",
    bids: Union[np.ndarray, list, None] = None,
    asks: Union[np.ndarray, list, None] = None,
    stats: tuple[int, int, int, int, float, float, float] | None = None,
    fused_stats: tuple[int, int, int, int, int, int, float] | None = None,
    is_snapshot: bool = False,
    seq: int = 1,
    source_ts: int = 0,
    local_ts: int = 0,
    topic: str = "",
) -> BidAskEvent:
    """Create a ``BidAskEvent`` with sensible defaults.

    Default book: single level bid 499.0 x 10 / ask 501.0 x 10 (scaled x10000).
    """
    if bids is None:
        bids = np.array([[4_990_000, 10]], dtype=np.int64)
    if asks is None:
        asks = np.array([[5_010_000, 10]], dtype=np.int64)
    return BidAskEvent(
        meta=_meta(seq=seq, source_ts=source_ts, local_ts=local_ts, topic=topic),
        symbol=symbol,
        bids=bids,
        asks=asks,
        stats=stats,
        fused_stats=fused_stats,
        is_snapshot=is_snapshot,
    )


def make_lob_stats_event(
    *,
    symbol: str = "2330",
    ts: int = 0,
    imbalance: float = 0.0,
    best_bid: int = 4_990_000,
    best_ask: int = 5_010_000,
    bid_depth: int = 100,
    ask_depth: int = 100,
    mid_price_x2: int | None = None,
    spread_scaled: int | None = None,
) -> LOBStatsEvent:
    """Create a ``LOBStatsEvent`` with sensible defaults.

    ``mid_price_x2`` and ``spread_scaled`` are derived from best_bid/best_ask
    automatically by ``LOBStatsEvent.__post_init__`` when left as ``None``.
    """
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts or timebase.now_ns(),
        imbalance=imbalance,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        mid_price_x2=mid_price_x2,
        spread_scaled=spread_scaled,
    )


def make_fill_event(
    *,
    fill_id: str = "fill-001",
    account_id: str = "test-account",
    order_id: str = "order-001",
    strategy_id: str = "s1",
    symbol: str = "2330",
    side: Side = Side.BUY,
    qty: int = 1,
    price: int = 5_000_000,
    fee: int = 200_000,
    tax: int = 0,
    ingest_ts_ns: int = 0,
    match_ts_ns: int = 0,
) -> FillEvent:
    """Create a ``FillEvent`` with sensible defaults.

    All monetary fields are scaled x10000.
    ``fee`` default 200_000 = 20.0 TWD commission.
    """
    now = timebase.now_ns()
    return FillEvent(
        fill_id=fill_id,
        account_id=account_id,
        order_id=order_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=ingest_ts_ns or now,
        match_ts_ns=match_ts_ns or now,
    )
