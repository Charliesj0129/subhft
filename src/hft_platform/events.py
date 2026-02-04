from dataclasses import dataclass
from typing import Union

import numpy as np


@dataclass(slots=True)
class MetaData:
    """Common metadata headers."""

    seq: int
    topic: str
    source_ts: int  # Exchange timestamp
    local_ts: int  # Ingestion timestamp


@dataclass(slots=True)
class TickEvent:
    """
    Standardized Tick Data.
    Replacing Dict-based structure in normalizer.py
    """

    meta: MetaData
    symbol: str
    price: int  # Scaled x10000
    volume: int  # Incremental volume
    total_volume: int
    bid_side_total_vol: int
    ask_side_total_vol: int

    # Optional flags (packed or individual?)
    is_simtrade: bool
    is_odd_lot: bool


@dataclass(slots=True)
class BidAskEvent:
    """
    L1/L5 Update.
    """

    meta: MetaData
    symbol: str
    # Bids/Asks as Numpy Arrays for vectorized LOB updates
    # Shape: (N, 2) -> [[Price, Volume], ...]
    # Dtype: np.int64 (to support large volumes/prices safely)
    bids: Union[np.ndarray, list]
    asks: Union[np.ndarray, list]
    stats: tuple[int, int, int, int, float, float, float] | None = None
    is_snapshot: bool = False


@dataclass(slots=True)
class LOBStatsEvent:
    """
    Derived LOB metrics emitted by LOBEngine.
    """

    symbol: str
    ts: int
    mid_price: float
    spread: float
    imbalance: float
    best_bid: int
    best_ask: int
    bid_depth: int
    ask_depth: int
