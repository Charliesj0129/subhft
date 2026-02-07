import asyncio
import importlib
import os
import time
from threading import Lock
from typing import Any, Dict, Optional, Union

import numpy as np
from structlog import get_logger

from hft_platform.events import BidAskEvent, LOBStatsEvent, TickEvent
from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("feed_adapter.lob")

_RUST_ENABLED = os.getenv("HFT_RUST_ACCEL", "1").lower() not in {"0", "false", "no", "off"}
_LOCKS_ENABLED = os.getenv("HFT_LOB_LOCKS", "0").lower() not in {"0", "false", "no", "off"}
_READ_LOCKS_ENABLED = os.getenv("HFT_LOB_READ_LOCKS", "1").lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_LOCAL_TS_ENABLED = os.getenv("HFT_LOB_LOCAL_TS", "0").lower() not in {"0", "false", "no", "off"}
_METRICS_ENABLED = os.getenv("HFT_METRICS_ENABLED", "0").lower() not in {"0", "false", "no", "off"}
_METRICS_BATCH = max(1, int(os.getenv("HFT_METRICS_BATCH", "4096")))
_METRICS_ASYNC = os.getenv("HFT_METRICS_ASYNC", "1").lower() not in {"0", "false", "no", "off"}
_STATS_MODE = os.getenv("HFT_LOB_STATS_MODE", "event").lower()
_STATS_TUPLE = _STATS_MODE in {"tuple", "raw"}
_STATS_NONE = _STATS_MODE in {"none", "off", "disabled"}
_FORCE_NUMPY = os.getenv("HFT_LOB_FORCE_NUMPY", "1").lower() not in {"0", "false", "no", "off"}

try:
    try:
        _rust_core = importlib.import_module("hft_platform.rust_core")
    except Exception:
        _rust_core = importlib.import_module("rust_core")

    _RUST_COMPUTE_STATS = _rust_core.compute_book_stats
except Exception as exc:
    logger.warning(
        "Rust compute_book_stats unavailable - using pure Python fallback",
        error=str(exc),
    )
    _RUST_COMPUTE_STATS = None


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


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
        "mid_price_x2",
        "spread",
        "imbalance",
        "last_price",
        "last_volume",
        "bid_depth_total",
        "ask_depth_total",
    )

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.lock = Lock() if _LOCKS_ENABLED else _NoopLock()

        # Shape: List[List[int]] or np.ndarray (N,2)
        self.bids: list[list[int]] | np.ndarray = []
        self.asks: list[list[int]] | np.ndarray = []

        self.exch_ts: int = 0
        self.local_ts: int = 0
        self.version: int = 0

        # Stats (mid_price_x2 = best_bid + best_ask, avoids division)
        self.mid_price_x2: int = 0
        self.spread: int = 0
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

            if _FORCE_NUMPY and _RUST_ENABLED:
                if not isinstance(bids, np.ndarray) and bids:
                    bids = np.asarray(bids, dtype=np.int64)
                if not isinstance(asks, np.ndarray) and asks:
                    asks = np.asarray(asks, dtype=np.int64)

            self.exch_ts = exch_ts
            if _LOCAL_TS_ENABLED:
                self.local_ts = time.time_ns()

            # Assign directly (allow numpy arrays for zero-copy stats computation)
            if isinstance(bids, np.ndarray):
                self.bids = bids if bids.size > 0 else []
            elif bids:
                self.bids = bids
            else:
                self.bids = []  # Ensure cleared if empty

            if isinstance(asks, np.ndarray):
                self.asks = asks if asks.size > 0 else []
            elif asks:
                self.asks = asks
            else:
                self.asks = []

            if not _STATS_NONE:
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
        # Rust fast path for numpy arrays
        if (
            _RUST_ENABLED
            and _RUST_COMPUTE_STATS
            and isinstance(self.bids, np.ndarray)
            and isinstance(self.asks, np.ndarray)
        ):
            try:
                (
                    best_bid,
                    best_ask,
                    bid_depth_total,
                    ask_depth_total,
                    mid_price,
                    spread,
                    imbalance,
                ) = _RUST_COMPUTE_STATS(self.bids, self.asks)
                self.bid_depth_total = int(bid_depth_total)
                self.ask_depth_total = int(ask_depth_total)
                self.mid_price_x2 = int(best_bid) + int(best_ask)
                self.spread = int(best_ask) - int(best_bid)
                self.imbalance = float(imbalance)
                return
            except Exception:
                pass

        # 1. Depth (Pure Python Sum)
        if isinstance(self.bids, np.ndarray):
            if self.bids.size > 0:
                self.bid_depth_total = int(self.bids[:, 1].sum())
                best_bid = int(self.bids[0, 0])
                bid_vol_top = int(self.bids[0, 1])
            else:
                self.bid_depth_total = 0
                best_bid = 0
                bid_vol_top = 0
        elif self.bids:
            self.bid_depth_total = sum(row[1] for row in self.bids)
            best_bid = self.bids[0][0]
            bid_vol_top = self.bids[0][1]
        else:
            self.bid_depth_total = 0
            best_bid = 0
            bid_vol_top = 0

        if isinstance(self.asks, np.ndarray):
            if self.asks.size > 0:
                self.ask_depth_total = int(self.asks[:, 1].sum())
                best_ask = int(self.asks[0, 0])
                ask_vol_top = int(self.asks[0, 1])
            else:
                self.ask_depth_total = 0
                best_ask = 0
                ask_vol_top = 0
        elif self.asks:
            self.ask_depth_total = sum(row[1] for row in self.asks)
            best_ask = self.asks[0][0]
            ask_vol_top = self.asks[0][1]
        else:
            self.ask_depth_total = 0
            best_ask = 0
            ask_vol_top = 0

        # 2. Price Stats
        if best_bid > 0 and best_ask > 0:
            self.mid_price_x2 = best_bid + best_ask
            self.spread = best_ask - best_bid

            # Imbalance (Top 1)
            total_top = bid_vol_top + ask_vol_top
            if total_top > 0:
                self.imbalance = (bid_vol_top - ask_vol_top) / total_top
            else:
                self.imbalance = 0.0
        else:
            self.mid_price_x2 = 0
            self.spread = 0
            self.imbalance = 0.0

    def get_stats(self) -> LOBStatsEvent:
        with self.lock:
            if isinstance(self.bids, np.ndarray):
                best_bid = int(self.bids[0, 0]) if self.bids.size > 0 else 0
            else:
                best_bid = int(self.bids[0][0]) if self.bids else 0

            if isinstance(self.asks, np.ndarray):
                best_ask = int(self.asks[0, 0]) if self.asks.size > 0 else 0
            else:
                best_ask = int(self.asks[0][0]) if self.asks else 0

            return LOBStatsEvent(
                symbol=self.symbol,
                ts=self.exch_ts,
                mid_price_x2=self.mid_price_x2,
                spread_scaled=self.spread,
                imbalance=self.imbalance,
                best_bid=best_bid,
                best_ask=best_ask,
                bid_depth=int(self.bid_depth_total),
                ask_depth=int(self.ask_depth_total),
            )

    def get_stats_tuple(self) -> tuple:
        with self.lock:
            if isinstance(self.bids, np.ndarray):
                best_bid = int(self.bids[0, 0]) if self.bids.size > 0 else 0
            else:
                best_bid = int(self.bids[0][0]) if self.bids else 0

            if isinstance(self.asks, np.ndarray):
                best_ask = int(self.asks[0, 0]) if self.asks.size > 0 else 0
            else:
                best_ask = int(self.asks[0][0]) if self.asks else 0

            return (
                self.symbol,
                self.exch_ts,
                self.mid_price_x2,  # Integer (best_bid + best_ask)
                self.spread,  # Integer (best_ask - best_bid)
                self.imbalance,
                best_bid,
                best_ask,
                int(self.bid_depth_total),
                int(self.ask_depth_total),
            )

    def apply_update_with_stats(
        self,
        bids: Union[np.ndarray, list],
        asks: Union[np.ndarray, list],
        exch_ts: int,
        stats: tuple[int, int, int, int, float, float, float],
    ) -> None:
        with self.lock:
            if exch_ts < self.exch_ts:
                return

            if _FORCE_NUMPY and _RUST_ENABLED:
                if not isinstance(bids, np.ndarray) and bids:
                    bids = np.asarray(bids, dtype=np.int64)
                if not isinstance(asks, np.ndarray) and asks:
                    asks = np.asarray(asks, dtype=np.int64)

            self.exch_ts = exch_ts
            if _LOCAL_TS_ENABLED:
                self.local_ts = time.time_ns()

            if isinstance(bids, np.ndarray):
                self.bids = bids if bids.size > 0 else []
            elif bids:
                self.bids = bids
            else:
                self.bids = []

            if isinstance(asks, np.ndarray):
                self.asks = asks if asks.size > 0 else []
            elif asks:
                self.asks = asks
            else:
                self.asks = []

            _best_bid, _best_ask, bid_depth, ask_depth, _mid, _spread, imbalance = stats
            self.bid_depth_total = int(bid_depth)
            self.ask_depth_total = int(ask_depth)
            self.mid_price_x2 = int(_best_bid) + int(_best_ask)
            self.spread = int(_best_ask) - int(_best_bid)
            self.imbalance = float(imbalance)
            self.version += 1


class LOBEngine:
    def __init__(self):
        self.books: Dict[str, BookState] = {}
        # Global lock removed!
        self.metrics = MetricsRegistry.get()
        self._metrics_enabled = _METRICS_ENABLED and self.metrics is not None
        self._metrics_batch = _METRICS_BATCH
        self._metrics_pending_updates: Dict[tuple[str, str], int] = {}
        self._metrics_pending_snapshots: Dict[str, int] = {}
        self._metrics_pending_total = 0
        self._metrics_flush_requested = False
        self._metrics_task: asyncio.Task | None = None
        self._last_symbol: str | None = None
        self._last_book: BookState | None = None

    def _is_metrics_enabled(self) -> bool:
        if self._metrics_enabled:
            return True
        if self.metrics is None:
            return False
        return not isinstance(self.metrics, MetricsRegistry)

    def start_metrics_worker(self, loop: asyncio.AbstractEventLoop, interval_ms: int = 5) -> None:
        if not _METRICS_ASYNC or self._metrics_task is not None:
            return

        async def _worker():
            try:
                while True:
                    await asyncio.sleep(interval_ms / 1000.0)
                    if self._metrics_pending_total <= 0 and not self._metrics_flush_requested:
                        continue
                    self._metrics_flush_requested = False
                    self._flush_metrics()
            except asyncio.CancelledError:
                pass

        self._metrics_task = loop.create_task(_worker())

    def _flush_metrics(self):
        if not self._is_metrics_enabled():
            return

        for (symbol, update_type), count in self._metrics_pending_updates.items():
            self.metrics.lob_updates_total.labels(symbol=symbol, type=update_type).inc(count)
        self._metrics_pending_updates.clear()

        for symbol, count in self._metrics_pending_snapshots.items():
            self.metrics.lob_snapshots_total.labels(symbol=symbol).inc(count)
        self._metrics_pending_snapshots.clear()

        self._metrics_pending_total = 0

    def _record_lob_metrics(self, symbol: str, is_snapshot: bool):
        if not self._is_metrics_enabled():
            return
        self._metrics_pending_updates[(symbol, "BidAsk")] = self._metrics_pending_updates.get((symbol, "BidAsk"), 0) + 1
        if is_snapshot:
            self._metrics_pending_snapshots[symbol] = self._metrics_pending_snapshots.get(symbol, 0) + 1
        self._metrics_pending_total += 1
        if self._metrics_pending_total >= self._metrics_batch:
            if _METRICS_ASYNC and isinstance(self.metrics, MetricsRegistry):
                self._metrics_flush_requested = True
            else:
                self._flush_metrics()
        elif not isinstance(self.metrics, MetricsRegistry):
            self._flush_metrics()

    def _emit_stats(self, book: BookState):
        if _STATS_NONE:
            return None
        if _STATS_TUPLE:
            return book.get_stats_tuple()
        return book.get_stats()

    def get_book(self, symbol: str) -> BookState:
        if symbol == self._last_symbol and self._last_book is not None:
            return self._last_book
        if symbol not in self.books:
            # First time might race if multithreaded init, but usually symbols known.
            # Lazy init needing global lock?
            # Or assume pre-warmed.
            # Let's put a small lock for dict mutation only.
            self.books[symbol] = BookState(symbol)
        book = self.books[symbol]
        self._last_symbol = symbol
        self._last_book = book
        return book

    def process_event(self, event: Union[BidAskEvent, TickEvent, tuple]) -> Optional[LOBStatsEvent | tuple]:
        metrics_enabled = self._is_metrics_enabled()
        # Tuple fast-path (avoid event object creation)
        if isinstance(event, tuple) and event:
            if event[0] == "bidask":
                if len(event) >= 13:
                    (
                        _,
                        symbol,
                        bids,
                        asks,
                        exch_ts,
                        is_snapshot,
                        best_bid,
                        best_ask,
                        bid_depth,
                        ask_depth,
                        mid_price,
                        spread,
                        imbalance,
                    ) = event[:13]
                    stats = (
                        int(best_bid),
                        int(best_ask),
                        int(bid_depth),
                        int(ask_depth),
                        float(mid_price),
                        float(spread),
                        float(imbalance),
                    )
                    book = self.get_book(symbol)
                    book.apply_update_with_stats(bids, asks, exch_ts, stats)
                else:
                    _, symbol, bids, asks, exch_ts, is_snapshot = event
                    book = self.get_book(symbol)
                    book.apply_update(bids, asks, exch_ts)
                if metrics_enabled:
                    self._record_lob_metrics(symbol, bool(is_snapshot))
                return self._emit_stats(book)
            if event[0] == "tick":
                _, symbol, price, volume, _total_volume, _is_simtrade, _is_odd_lot, exch_ts = event
                book = self.get_book(symbol)
                book.update_tick(price, volume, exch_ts)
                return None

        # Typed dispatch
        if isinstance(event, BidAskEvent):
            book = self.get_book(event.symbol)
            if event.stats is not None:
                book.apply_update_with_stats(event.bids, event.asks, event.meta.source_ts, event.stats)
            else:
                book.apply_update(event.bids, event.asks, event.meta.source_ts)
            if metrics_enabled:
                self._record_lob_metrics(event.symbol, event.is_snapshot)
            return self._emit_stats(book)

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

        if _READ_LOCKS_ENABLED:
            lock_ctx = book.lock
        else:
            lock_ctx = _NoopLock()

        with lock_ctx:
            # Convert Numpy to list of dicts for compatibility
            # Or just simple top level?
            # BaseStrategy.get_l1 usually expects something.

            # Safely handle empty arrays
            if isinstance(book.bids, np.ndarray):
                best_bid = int(book.bids[0][0]) if book.bids.size > 0 else 0
            else:
                best_bid = int(book.bids[0][0]) if book.bids else 0

            if isinstance(book.asks, np.ndarray):
                best_ask = int(book.asks[0][0]) if book.asks.size > 0 else 0
            else:
                best_ask = int(book.asks[0][0]) if book.asks else 0

            return {
                "symbol": symbol,
                "timestamp": book.exch_ts,
                "best_bid": best_bid,
                "best_ask": best_ask,
                # Backward-compatible float fields (scaled units)
                "mid_price": book.mid_price_x2 / 2.0,
                "spread": float(book.spread),
                # Strict integer fields
                "mid_price_x2": book.mid_price_x2,  # Scaled integer (best_bid + best_ask)
                "spread_scaled": book.spread,  # Scaled integer (best_ask - best_bid)
            }
