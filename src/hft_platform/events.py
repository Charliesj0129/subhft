from dataclasses import dataclass
from typing import Union

import numpy as np


@dataclass(slots=True)
class MetaData:
    """Common metadata headers."""

    seq: int
    source_ts: int  # Exchange timestamp
    local_ts: int  # Ingestion timestamp
    topic: str = ""


# Backward-compatible alias expected by integration tests.
TickMeta = MetaData


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
    total_volume: int = 0
    bid_side_total_vol: int = 0
    ask_side_total_vol: int = 0

    # Optional flags (packed or individual?)
    is_simtrade: bool = False
    is_odd_lot: bool = False


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

    Precision Law: All price values stored as scaled integers (x10000 default).
    For backward compatibility, mid_price/spread are still exposed as floats
    in scaled units. Prefer mid_price_x2/spread_scaled for strict integer math.
    """

    symbol: str
    ts: int
    imbalance: float  # Ratio [-1, 1], float acceptable for bounded ratios
    best_bid: int
    best_ask: int
    bid_depth: int
    ask_depth: int
    # Backward-compatible fields (scaled floats)
    mid_price: float | None = None
    spread: float | None = None
    # Strict integer fields (preferred)
    mid_price_x2: int | None = None  # best_bid + best_ask (divide by 2 for mid price)
    spread_scaled: int | None = None  # best_ask - best_bid (scaled integer)

    def __post_init__(self) -> None:
        if self.mid_price_x2 is None:
            if self.best_bid and self.best_ask:
                self.mid_price_x2 = int(self.best_bid) + int(self.best_ask)
            elif self.mid_price is not None:
                self.mid_price_x2 = int(round(self.mid_price * 2))
            else:
                self.mid_price_x2 = 0
        if self.mid_price is None:
            self.mid_price = self.mid_price_x2 / 2.0
        else:
            self.mid_price = float(self.mid_price)

        if self.spread_scaled is None:
            if self.best_bid and self.best_ask:
                self.spread_scaled = int(self.best_ask) - int(self.best_bid)
            elif self.spread is not None:
                self.spread_scaled = int(round(self.spread))
            else:
                self.spread_scaled = 0
        if self.spread is None:
            self.spread = float(self.spread_scaled)
        else:
            self.spread = float(self.spread)

    @property
    def mid_price_scaled(self) -> int:
        """Returns mid price as scaled integer (truncated)."""
        return (self.mid_price_x2 or 0) // 2

    @property
    def mid_price_float(self) -> float:
        """Returns mid price as float (for display/logging only)."""
        return (self.mid_price_x2 or 0) / 2.0
