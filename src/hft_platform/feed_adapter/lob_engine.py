import time
from threading import Lock
from typing import Any, Dict, Optional, Union

import numpy as np
from structlog import get_logger

from hft_platform.events import BidAskEvent, LOBStatsEvent, TickEvent
from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("feed_adapter.lob")


class BookState:
    """
    Per-symbol LOB state using Numpy for latency.
    """

    __slots__ = (
        "symbol",
        "lock",
        "bids",
        "asks",
        "exch_ts",
        "local_ts",
        "version",
        "mid_price",
        "spread",
        "imbalance",
        "last_price",
        "last_volume",
        "bid_depth_total",
        "ask_depth_total",
    )

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.lock = Lock()  # Per-symbol lock

        # Shape: List[List[int]] [[Price, Vol], ...]
        self.bids: list[list[int]] = []
        self.asks: list[list[int]] = []

        self.exch_ts: int = 0
        self.local_ts: int = 0
        self.version: int = 0

        # Stats
        self.mid_price: float = 0.0
        self.spread: float = 0.0
        self.imbalance: float = 0.0
        self.last_price: int = 0
        self.last_volume: int = 0
        self.bid_depth_total: int = 0
        self.ask_depth_total: int = 0

    def apply_update(self, bids: Union[np.ndarray, list], asks: Union[np.ndarray, list], exch_ts: int):
        """Atomic update (Snapshot style full-replace for Top-N streams)."""
        with self.lock:
            if exch_ts < self.exch_ts:
                # Late packet
                return

            self.exch_ts = exch_ts
            self.local_ts = time.time_ns()

            # Assign directly (assuming list or compatible iterable)
            # If incoming is numpy, list() converts it but slow?
            # Normalizer now sends list.
            if isinstance(bids, np.ndarray):
                if bids.size > 0:
                    self.bids = bids.tolist()
            elif bids:
                self.bids = bids
            else:
                self.bids = []  # Ensure cleared if empty

            if isinstance(asks, np.ndarray):
                if asks.size > 0:
                    self.asks = asks.tolist()
            elif asks:
                self.asks = asks
            else:
                self.asks = []

            self._recompute()
            self.version += 1

    def update_tick(self, price: int, volume: int, exch_ts: int):
        with self.lock:
            if exch_ts < self.exch_ts:
                return

            self.exch_ts = exch_ts
            self.last_price = price
            self.last_volume = volume

    def _recompute(self):
        """Vectorized stats computation."""
        # 1. Depth (Pure Python Sum)
        if self.bids:
            self.bid_depth_total = sum(row[1] for row in self.bids)
            best_bid = self.bids[0][0]
            bid_vol_top = self.bids[0][1]
        else:
            self.bid_depth_total = 0
            best_bid = 0
            bid_vol_top = 0

        if self.asks:
            self.ask_depth_total = sum(row[1] for row in self.asks)
            best_ask = self.asks[0][0]
            ask_vol_top = self.asks[0][1]
        else:
            self.ask_depth_total = 0
            best_ask = 0
            ask_vol_top = 0

        # 2. Price Stats
        if best_bid > 0 and best_ask > 0:
            self.mid_price = (best_bid + best_ask) / 2.0
            self.spread = float(best_ask - best_bid)

            # Imbalance (Top 1)
            total_top = bid_vol_top + ask_vol_top
            if total_top > 0:
                self.imbalance = (bid_vol_top - ask_vol_top) / total_top
            else:
                self.imbalance = 0.0
        else:
            self.mid_price = 0.0
            self.spread = 0.0
            self.imbalance = 0.0

    def get_stats(self) -> LOBStatsEvent:
        with self.lock:
            return LOBStatsEvent(
                symbol=self.symbol,
                ts=self.exch_ts,
                mid_price=self.mid_price,
                spread=self.spread,
                imbalance=self.imbalance,
                best_bid=int(self.bids[0][0]) if self.bids else 0,
                best_ask=int(self.asks[0][0]) if self.asks else 0,
                bid_depth=int(self.bid_depth_total),
                ask_depth=int(self.ask_depth_total),
            )


class LOBEngine:
    def __init__(self):
        self.books: Dict[str, BookState] = {}
        # Global lock removed!
        self.metrics = MetricsRegistry.get()

    def get_book(self, symbol: str) -> BookState:
        if symbol not in self.books:
            # First time might race if multithreaded init, but usually symbols known.
            # Lazy init needing global lock?
            # Or assume pre-warmed.
            # Let's put a small lock for dict mutation only.
            self.books[symbol] = BookState(symbol)
        return self.books[symbol]

    def process_event(self, event: Union[BidAskEvent, TickEvent]) -> Optional[LOBStatsEvent]:
        # Typed dispatch
        if isinstance(event, BidAskEvent):
            book = self.get_book(event.symbol)
            book.apply_update(event.bids, event.asks, event.meta.source_ts)
            if self.metrics:
                self.metrics.lob_updates_total.labels(symbol=event.symbol, type="BidAsk").inc()
                if event.is_snapshot:
                    self.metrics.lob_snapshots_total.labels(symbol=event.symbol).inc()
            return book.get_stats()

        elif isinstance(event, TickEvent):
            book = self.get_book(event.symbol)
            book.update_tick(event.price, event.volume, event.meta.source_ts)
            # return book.get_stats() # Optional: emit stats on tick?
            return None

        return None

    def get_book_snapshot(self, symbol: str) -> Optional[Dict[str, Any]]:
        if symbol not in self.books:
            return None
        book = self.books[symbol]

        with book.lock:
            # Convert Numpy to list of dicts for compatibility
            # Or just simple top level?
            # BaseStrategy.get_l1 usually expects something.

            # Safely handle empty arrays
            best_bid = int(book.bids[0][0]) if book.bids else 0
            best_ask = int(book.asks[0][0]) if book.asks else 0

            return {
                "symbol": symbol,
                "timestamp": book.exch_ts,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": book.spread,
                "mid_price": book.mid_price,
            }
