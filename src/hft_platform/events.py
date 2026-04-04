from dataclasses import dataclass
from typing import NamedTuple, Union

import numpy as np


class BookStats(NamedTuple):
    """Named stats tuple for BidAskEvent.stats (backward-compat: floats for mid_price/spread)."""

    best_bid: int
    best_ask: int
    bid_depth: int
    ask_depth: int
    mid_price: float  # mid_price_x2 / 2.0 — backward compat float
    spread: float  # spread as float
    imbalance: float


class FusedBookStats(NamedTuple):
    """Named stats tuple for BidAskEvent.fused_stats (integers for LOBEngine bypass)."""

    best_bid: int
    best_ask: int
    bid_depth: int
    ask_depth: int
    mid_price_x2: int  # integer: best_bid + best_ask
    spread_scaled: int  # integer: best_ask - best_bid
    imbalance: float


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

    # EMO trade classification: +1=BUY, -1=SELL, 0=UNKNOWN
    trade_direction: int = 0
    # Classification confidence (scaled x1000): 1000=at-quote, 800=inside, 500=tick-rule, 0=unknown
    trade_confidence: int = 0


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
    stats: BookStats | None = None
    fused_stats: FusedBookStats | None = None
    is_snapshot: bool = False


@dataclass(slots=True)
class LOBStatsEvent:
    """
    Derived LOB metrics emitted by LOBEngine.

    Precision Law: All price values stored as scaled integers (x10000 default).
    mid_price and spread are lazy properties (backward-compat, display/logging only).
    Prefer mid_price_x2/spread_scaled for strict integer math on the hot path.
    """

    symbol: str
    ts: int
    imbalance: float  # Ratio [-1, 1], float acceptable for bounded ratios
    best_bid: int
    best_ask: int
    bid_depth: int
    ask_depth: int
    # Strict integer fields (preferred)
    mid_price_x2: int | None = None  # best_bid + best_ask (divide by 2 for mid price)
    spread_scaled: int | None = None  # best_ask - best_bid (scaled integer)

    def __post_init__(self) -> None:
        # Compute integer fields if not provided
        if self.mid_price_x2 is None:
            if self.best_bid is not None and self.best_ask is not None:
                self.mid_price_x2 = int(self.best_bid) + int(self.best_ask)
            else:
                self.mid_price_x2 = 0
        if self.spread_scaled is None:
            if self.best_bid is not None and self.best_ask is not None:
                self.spread_scaled = int(self.best_ask) - int(self.best_bid)
            else:
                self.spread_scaled = 0

    @property
    def mid_price(self) -> float:
        """Backward-compat float mid price (for display/logging only)."""
        return (self.mid_price_x2 or 0) / 2.0

    @property
    def spread(self) -> float:
        """Backward-compat float spread (for display/logging only)."""
        return float(self.spread_scaled or 0)

    @property
    def mid_price_scaled(self) -> int:
        """Returns mid price as scaled integer (truncated)."""
        return (self.mid_price_x2 or 0) // 2

    @property
    def mid_price_float(self) -> float:
        """Returns mid price as float (for display/logging only)."""
        return (self.mid_price_x2 or 0) / 2.0


@dataclass(slots=True)
class FeatureUpdateEvent:
    """Shared feature-plane update emitted after LOB/feature processing (prototype ABI v1)."""

    symbol: str
    ts: int  # source timestamp ns
    local_ts: int
    seq: int
    feature_set_id: str
    schema_version: int
    changed_mask: int
    warmup_ready_mask: int
    quality_flags: int
    feature_ids: tuple[str, ...]
    values: tuple[int | float, ...]

    def get(self, feature_id: str) -> int | float | None:
        try:
            idx = self.feature_ids.index(str(feature_id))
        except ValueError:
            return None
        if idx >= len(self.values):
            return None
        return self.values[idx]

    def to_typed_frame(self):
        from hft_platform.feature.boundary import event_to_typed_frame

        return event_to_typed_frame(self)


@dataclass(slots=True)
class GapEvent:
    """Injected when RingBufferBus consumer detects overflow (skip-forward).

    Strategies receiving this event should assume that ``missed_count`` events
    were silently dropped and react accordingly (e.g. reset stale state,
    re-request LOB snapshot).
    """

    missed_count: int
    first_missed_seq: int
    last_missed_seq: int
    ts: int  # nanoseconds from timebase
