import asyncio
import importlib
import os
from threading import Lock
from typing import Any, Dict, Optional, Union

import numpy as np
from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.events import BidAskEvent, BookStats, LOBStatsEvent, TickEvent
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
# HFT_STATS_TUPLE=1 is a convenience alias for HFT_LOB_STATS_MODE=tuple
_STATS_TUPLE_ENV = os.getenv("HFT_STATS_TUPLE", "0") == "1"
_STATS_TUPLE = _STATS_MODE in {"tuple", "raw"} or _STATS_TUPLE_ENV
_STATS_NONE = _STATS_MODE in {"none", "off", "disabled"}
_FORCE_NUMPY = os.getenv("HFT_LOB_FORCE_NUMPY", "1").lower() not in {"0", "false", "no", "off"}

_RUST_BOOK_STATE_ENABLED = os.getenv("HFT_LOB_RUST_BOOKSTATE", "1").lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_FUSED_BYPASS = os.environ.get("HFT_FUSED_NORMALIZER", "0") == "1"

try:
    try:
        _rust_core = importlib.import_module("hft_platform.rust_core")
    except ImportError:
        _rust_core = importlib.import_module("rust_core")

    _RUST_COMPUTE_STATS = _rust_core.compute_book_stats
    _RustBookState = getattr(_rust_core, "RustBookState", None)
except Exception as exc:
    logger.warning(
        "Rust compute_book_stats unavailable - using pure Python fallback",
        error=str(exc),
    )
    _RUST_COMPUTE_STATS = None
    _RustBookState = None


class _NoopLock:
    __slots__ = ()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
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
        "normalizer_seq",
        "_rust_state",
        "_cached_stats",
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
        self.normalizer_seq: int = 0

        # _cached_stats retained as a slot placeholder; no longer used at runtime
        # (get_stats() now creates a new LOBStatsEvent per tick to avoid shared-ref corruption).
        self._cached_stats: Any = None

        # Rust-accelerated book state (opt-in)
        self._rust_state: Any = None
        if _RUST_BOOK_STATE_ENABLED and _RustBookState is not None:
            try:
                self._rust_state = _RustBookState(symbol)
            except Exception as exc:
                logger.debug("rust_book_state_init_failed", symbol=symbol, error=str(exc))

    def apply_update(self, bids: Union[np.ndarray, list], asks: Union[np.ndarray, list], exch_ts: int):
        """Atomic update (Snapshot style full-replace for Top-N streams)."""
        with self.lock:
            if exch_ts > 0 and exch_ts < self.exch_ts:
                # Late packet — skip stale data
                return
            # DATA-007: Preserve valid timestamp when new update has ts=0.
            if exch_ts == 0 and self.exch_ts > 0:
                exch_ts = self.exch_ts

            if _FORCE_NUMPY and _RUST_ENABLED:
                if isinstance(bids, np.ndarray):
                    if bids.dtype != np.int64:
                        bids = bids.astype(np.int64, copy=False)
                elif bids:
                    bids = np.asarray(bids, dtype=np.int64)
                if isinstance(asks, np.ndarray):
                    if asks.dtype != np.int64:
                        asks = asks.astype(np.int64, copy=False)
                elif asks:
                    asks = np.asarray(asks, dtype=np.int64)

            self.exch_ts = exch_ts
            if _LOCAL_TS_ENABLED:
                self.local_ts = timebase.now_ns()

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
                # Rust fast path: delegate entire recompute to RustBookState
                rs = self._rust_state
                if rs is not None and isinstance(self.bids, np.ndarray) and isinstance(self.asks, np.ndarray):
                    try:
                        bids_2d = self.bids.reshape(-1, 2) if self.bids.ndim == 1 else self.bids
                        asks_2d = self.asks.reshape(-1, 2) if self.asks.ndim == 1 else self.asks
                        rs.apply_update(bids_2d, asks_2d, exch_ts)
                        # Crossed-book guard: Shioaji can emit best_bid > best_ask
                        # during auction transitions. Zero out stats to prevent
                        # negative spread propagating into FeatureEngine EMA state.
                        if rs.spread < 0:
                            self.mid_price_x2 = 0
                            self.spread = 0
                            self.imbalance = 0.0
                        else:
                            self.mid_price_x2 = rs.mid_price_x2
                            self.spread = rs.spread
                            self.imbalance = rs.imbalance
                        self.bid_depth_total = rs.bid_depth_total
                        self.ask_depth_total = rs.ask_depth_total
                        self.version += 1
                        return
                    except Exception as exc:
                        logger.debug("rust_book_state_update_fallback", symbol=self.symbol, error=str(exc))
                self._recompute()
            self.version += 1

    def update_tick(self, price: int, volume: int, exch_ts: int):
        with self.lock:
            if exch_ts > 0 and exch_ts < self.exch_ts:
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
                bb = int(best_bid)
                ba = int(best_ask)
                if bb > 0 and ba > 0 and ba >= bb:
                    self.mid_price_x2 = bb + ba
                    self.spread = ba - bb
                    self.imbalance = float(imbalance)
                else:
                    self.mid_price_x2 = 0
                    self.spread = 0
                    self.imbalance = 0.0
                return
            except Exception as exc:
                logger.debug("rust_compute_stats_fallback", symbol=self.symbol, error=str(exc))

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
        # Guard against crossed book (best_bid > best_ask), which Shioaji can
        # temporarily emit during auction transitions. Treat as invalid — same
        # as the else branch — to prevent negative spread propagating into
        # FeatureEngine EMA state.
        if best_bid > 0 and best_ask > 0 and best_ask >= best_bid:
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

            # Always create a new LOBStatsEvent per tick.
            # Previously the same object was mutated in-place and returned; multiple
            # consumers (StrategyRunner, RecorderService, FeatureEngine) held the same
            # reference, so the next tick's mutation corrupted data still being read.
            # A slotted dataclass with only primitive fields is cheap to allocate;
            # correctness outweighs the minor allocation cost here.
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
                normalizer_seq=self.normalizer_seq,
            )

    def get_stats_tuple(self) -> tuple:
        with self.lock:
            rs = self._rust_state
            if rs is not None:
                try:
                    rust_t = rs.get_stats_tuple()
                    return ("lobstats",) + rust_t
                except Exception as exc:
                    logger.debug("rust_stats_tuple_fallback", symbol=self.symbol, error=str(exc))
            if isinstance(self.bids, np.ndarray):
                best_bid = int(self.bids[0, 0]) if self.bids.size > 0 else 0
            else:
                best_bid = int(self.bids[0][0]) if self.bids else 0

            if isinstance(self.asks, np.ndarray):
                best_ask = int(self.asks[0, 0]) if self.asks.size > 0 else 0
            else:
                best_ask = int(self.asks[0][0]) if self.asks else 0

            return (
                "lobstats",  # [0] tag for runner tuple guard
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
        stats: BookStats,
    ) -> None:
        self.apply_update_with_stats_fields(bids, asks, exch_ts, *stats)

    def apply_update_with_stats_fields(
        self,
        bids: Union[np.ndarray, list],
        asks: Union[np.ndarray, list],
        exch_ts: int,
        best_bid: int,
        best_ask: int,
        bid_depth: int,
        ask_depth: int,
        _mid_price: float,
        _spread: float,
        imbalance: float,
    ) -> None:
        with self.lock:
            if exch_ts > 0 and exch_ts < self.exch_ts:
                return

            if _FORCE_NUMPY and _RUST_ENABLED:
                if isinstance(bids, np.ndarray):
                    if bids.dtype != np.int64:
                        bids = bids.astype(np.int64, copy=False)
                elif bids:
                    bids = np.asarray(bids, dtype=np.int64)
                if isinstance(asks, np.ndarray):
                    if asks.dtype != np.int64:
                        asks = asks.astype(np.int64, copy=False)
                elif asks:
                    asks = np.asarray(asks, dtype=np.int64)

            self.exch_ts = exch_ts
            if _LOCAL_TS_ENABLED:
                self.local_ts = timebase.now_ns()

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

            self.bid_depth_total = int(bid_depth)
            self.ask_depth_total = int(ask_depth)
            _bid = int(best_bid)
            _ask = int(best_ask)
            if _bid > 0 and _ask > 0 and _ask >= _bid:
                self.mid_price_x2 = _bid + _ask
                self.spread = _ask - _bid
                self.imbalance = float(imbalance)
            else:
                self.mid_price_x2 = 0
                self.spread = 0
                self.imbalance = 0.0
            self.version += 1


class LOBEngine:
    __slots__ = (
        "books",
        "metrics",
        "_metrics_enabled",
        "_metrics_batch",
        "_metrics_pending_updates",
        "_metrics_pending_snapshots",
        "_metrics_pending_total",
        "_metrics_flush_requested",
        "_metrics_task",
        "_last_symbol",
        "_last_book",
        "feature_engine",
        "_max_symbols",
        "_metrics_max_label_symbols",
        "_metrics_known_symbols",
        "_eviction_ttl_ns",
        "_eviction_last_run_ns",
    )

    def __init__(self):
        self.books: Dict[str, BookState] = {}
        self.feature_engine: Any = None
        self._max_symbols: int = int(os.getenv("HFT_EXPOSURE_MAX_SYMBOLS", "10000"))
        # Prometheus label cardinality guard (INFRA-05)
        self._metrics_max_label_symbols: int = int(os.getenv("HFT_METRICS_MAX_LABEL_SYMBOLS", "200"))
        self._metrics_known_symbols: set[str] = set()
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
        _evict_ttl_s = int(os.getenv("HFT_LOB_SYMBOL_TTL_S", "3600"))
        self._eviction_ttl_ns: int = _evict_ttl_s * 1_000_000_000
        self._eviction_last_run_ns: int = 0

    def _is_metrics_enabled(self) -> bool:
        if self._metrics_enabled:
            return True
        if self.metrics is None:
            return False
        return not isinstance(self.metrics, MetricsRegistry)

    def start_metrics_worker(self, loop: asyncio.AbstractEventLoop, interval_ms: int = 5) -> None:
        if not _METRICS_ASYNC or not _METRICS_ENABLED or self._metrics_task is not None:
            return

        async def _worker():
            try:
                while True:
                    await asyncio.sleep(interval_ms / 1000.0)
                    if self._metrics_pending_total <= 0 and not self._metrics_flush_requested:
                        continue
                    self._metrics_flush_requested = False
                    self._flush_metrics()
                    self.evict_stale_symbols()
            except asyncio.CancelledError:
                pass

        self._metrics_task = loop.create_task(_worker())

    def stop(self) -> None:
        """Cancel the background metrics worker task.

        Must be called on shutdown (e.g., from MarketDataService.run() finally block)
        to prevent resource leaks and post-shutdown state mutation.
        """
        task = self._metrics_task
        if task is not None:
            task.cancel()

    def _flush_metrics(self):
        if not self._is_metrics_enabled():
            return

        for (symbol, update_type), count in self._metrics_pending_updates.items():
            self.metrics.lob_updates_total.labels(symbol=self.metrics.cap_symbol(symbol), type=update_type).inc(count)
        self._metrics_pending_updates.clear()

        for symbol, count in self._metrics_pending_snapshots.items():
            self.metrics.lob_snapshots_total.labels(symbol=self.metrics.cap_symbol(symbol)).inc(count)
        self._metrics_pending_snapshots.clear()

        self._metrics_pending_total = 0

    def get_mid_price(self, symbol: str) -> int | None:
        """Return mid-price (scaled x10000) for *symbol*, or None if unavailable.

        Used by MarkToMarketCalculator for unrealized PnL computation.
        Returns ``mid_price_x2 // 2`` from the symbol's BookState.
        """
        book = self.books.get(symbol)
        if book is None or book.mid_price_x2 == 0:
            return None
        return book.mid_price_x2 // 2

    def reset_books(self) -> None:
        """Clear all book state. Call on broker reconnect to prevent stale LOB data."""
        self.books.clear()
        self._last_symbol = None
        self._last_book = None
        logger.info("lob_books_reset", reason="reconnect")

    def reset_books_for_symbols(self, symbols: set[str]) -> None:
        for sym in symbols:
            self.books.pop(sym, None)
        if self._last_symbol in symbols:
            self._last_symbol = None
            self._last_book = None

    def evict_stale_symbols(self) -> int:
        """Remove symbols whose last exchange timestamp is older than TTL.

        Returns the number of evicted symbols. Safe to call from the metrics
        worker or any periodic maintenance loop.
        """
        if self._eviction_ttl_ns <= 0:
            return 0
        now_ns = timebase.now_ns()
        # Rate-limit: run at most once per minute
        if now_ns - self._eviction_last_run_ns < 60_000_000_000:
            return 0
        self._eviction_last_run_ns = now_ns
        cutoff_ns = now_ns - self._eviction_ttl_ns
        stale = [sym for sym, book in self.books.items() if book.exch_ts > 0 and book.exch_ts < cutoff_ns]
        for sym in stale:
            del self.books[sym]
        if stale:
            # Clear single-entry cache if evicted symbol was cached
            if self._last_symbol in stale:
                self._last_symbol = None
                self._last_book = None
            logger.info(
                "lob_stale_symbols_evicted",
                count=len(stale),
                symbols=stale[:5],  # log at most 5 for brevity
            )
        return len(stale)

    def _record_lob_metrics(self, symbol: str, is_snapshot: bool):
        if not self._is_metrics_enabled():
            return
        # Guard Prometheus label cardinality (INFRA-05)
        if symbol not in self._metrics_known_symbols:
            if len(self._metrics_known_symbols) >= self._metrics_max_label_symbols:
                symbol = "_other"
            else:
                self._metrics_known_symbols.add(symbol)
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

    def get_book(self, symbol: str) -> Optional[BookState]:
        if symbol == self._last_symbol and self._last_book is not None:
            return self._last_book
        if symbol not in self.books:
            if len(self.books) >= self._max_symbols:
                logger.warning(
                    "lob_symbol_cardinality_exceeded",
                    current=len(self.books),
                    limit=self._max_symbols,
                    symbol=symbol,
                )
                return None
            self.books[symbol] = BookState(symbol)
        book = self.books[symbol]
        self._last_symbol = symbol
        self._last_book = book
        return book

    def process_event(self, event: Union[BidAskEvent, TickEvent, tuple]) -> Optional[LOBStatsEvent | tuple]:
        metrics_enabled = self._is_metrics_enabled()
        # Dispatch by exact type (avoid isinstance MRO walk). Event dataclasses
        # are frozen+slots with no subclasses — `type(x) is C` is correct.
        et = type(event)
        # Tuple fast-path (avoid event object creation)
        if et is tuple and event:
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
                    book = self.get_book(symbol)
                    if book is None:
                        return None
                    book.apply_update_with_stats_fields(
                        bids,
                        asks,
                        exch_ts,
                        int(best_bid),
                        int(best_ask),
                        int(bid_depth),
                        int(ask_depth),
                        float(mid_price),
                        float(spread),
                        float(imbalance),
                    )
                else:
                    _, symbol, bids, asks, exch_ts, is_snapshot = event
                    book = self.get_book(symbol)
                    if book is None:
                        return None
                    book.apply_update(bids, asks, exch_ts)
                if metrics_enabled:
                    self._record_lob_metrics(symbol, bool(is_snapshot))
                return self._emit_stats(book)
            if event[0] == "tick":
                _, symbol, price, volume, _total_volume, _is_simtrade, _is_odd_lot, exch_ts, *_rest = event
                book = self.get_book(symbol)
                if book is None:
                    return None
                book.update_tick(price, volume, exch_ts)
                return None

        # Typed dispatch (exact-type; subclasses never created for these)
        if et is BidAskEvent:
            book = self.get_book(event.symbol)
            if book is None:
                return None
            # Propagate normalizer seq for downstream ordering (Bug #10 fix)
            book.normalizer_seq = event.meta.seq
            # Fused bypass: normalizer already computed stats in a single Rust call;
            # skip redundant apply_update + _recompute and directly set book fields.
            if _FUSED_BYPASS and event.fused_stats is not None:
                fs = event.fused_stats
                exch_ts = event.meta.source_ts
                with book.lock:
                    if exch_ts >= book.exch_ts:
                        book.exch_ts = exch_ts
                        book.bids = event.bids
                        book.asks = event.asks
                        book.bid_depth_total = int(fs.bid_depth)
                        book.ask_depth_total = int(fs.ask_depth)
                        _fs_mid = int(fs.mid_price_x2)
                        _fs_sp = int(fs.spread_scaled)
                        if _fs_sp >= 0 and _fs_mid > 0:
                            book.mid_price_x2 = _fs_mid
                            book.spread = _fs_sp
                            book.imbalance = float(fs.imbalance)
                        else:
                            book.mid_price_x2 = 0
                            book.spread = 0
                            book.imbalance = 0.0
                        book.version += 1
                if metrics_enabled:
                    self._record_lob_metrics(event.symbol, event.is_snapshot)
                return self._emit_stats(book)
            if event.stats is not None:
                book.apply_update_with_stats(event.bids, event.asks, event.meta.source_ts, event.stats)
            else:
                book.apply_update(event.bids, event.asks, event.meta.source_ts)
            if metrics_enabled:
                self._record_lob_metrics(event.symbol, event.is_snapshot)
            return self._emit_stats(book)

        elif et is TickEvent:
            book = self.get_book(event.symbol)
            if book is None:
                return None
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

    def get_l1_scaled(self, symbol: str) -> Optional[tuple[int, int, int, int, int, int, int]]:
        """
        Low-allocation L1 snapshot for strategy hot path.

        Returns:
            (timestamp_ns, best_bid, best_ask, mid_price_x2, spread_scaled, bid_depth, ask_depth)
        All price fields are scaled integers.
        """
        if symbol not in self.books:
            return None
        book = self.books[symbol]

        lock_ctx = book.lock if _READ_LOCKS_ENABLED else _NoopLock()
        with lock_ctx:
            rs = book._rust_state
            if rs is not None:
                try:
                    return rs.get_l1_scaled()
                except Exception as exc:
                    logger.debug("rust_l1_scaled_fallback", symbol=symbol, error=str(exc))
            if isinstance(book.bids, np.ndarray):
                best_bid = int(book.bids[0, 0]) if book.bids.size > 0 else 0
            else:
                best_bid = int(book.bids[0][0]) if book.bids else 0

            if isinstance(book.asks, np.ndarray):
                best_ask = int(book.asks[0, 0]) if book.asks.size > 0 else 0
            else:
                best_ask = int(book.asks[0][0]) if book.asks else 0

            return (
                int(book.exch_ts),
                best_bid,
                best_ask,
                int(book.mid_price_x2),
                int(book.spread),
                int(book.bid_depth_total),
                int(book.ask_depth_total),
            )
