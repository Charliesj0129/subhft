from typing import Dict, Any, List, Optional
from threading import Lock
import time
from structlog import get_logger
from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("feed_adapter.lob")

class BookState:
    """
    Per-symbol LOB state. 
    Maintains top-5 bids/asks and derived stats.
    """
    def __init__(self, symbol: str):
        self.symbol = symbol
        # Use lists of dicts for now: [{"price": int, "volume": int}, ...]
        # Prices are scaled integers (x10000).
        self.bids: List[Dict[str, int]] = []
        self.asks: List[Dict[str, int]] = []
        
        # Metadata
        self.exch_ts: int = 0
        self.local_ts: int = 0
        self.version: int = 0
        self.degraded: bool = False
        
        # Derived Features
        self.mid_price: float = 0.0
        self.spread: float = 0.0
        self.imbalance: float = 0.0
        self.last_price: int = 0
        self.last_volume: int = 0
        self.bid_depth_total: int = 0
        self.ask_depth_total: int = 0

    def apply_snapshot(self, bids: List[Dict[str, int]], asks: List[Dict[str, int]], exch_ts: int):
        """Atomic snapshot application."""
        self.bids = bids
        self.asks = asks
        self.exch_ts = exch_ts
        self.version += 1
        self.local_ts = time.time_ns()
        self._recompute()

    def update_incremental(self, bids: List[Dict[str, int]], asks: List[Dict[str, int]], exch_ts: int):
        """
        Update incremental levels. 
        Shioaji often sends full top-5 arrays in streaming updates.
        """
        # Monotonicity check
        if exch_ts < self.exch_ts:
            # Out of order: mark degraded/warn
            # self.degraded = True # Strictness configurable
            pass
        
        self.exch_ts = exch_ts
        
        # Full replace of top-5 as supported by typical Shioaji stream
        if bids: self.bids = bids
        if asks: self.asks = asks
        
        self._recompute()

    def update_tick(self, price: int, volume: int, exch_ts: int):
        """Update trade info."""
        if exch_ts >= self.exch_ts:
             self.exch_ts = exch_ts
        
        self.last_price = price
        self.last_volume = volume
        # Note: Tick usually doesn't change LOB levels in this feed model (separate streams)

    def _recompute(self):
        """Compute derived features."""
        best_bid = self.bids[0]["price"] if self.bids else 0
        best_ask = self.asks[0]["price"] if self.asks else 0
        
        # Depth Totals
        self.bid_depth_total = sum(d["volume"] for d in self.bids)
        self.ask_depth_total = sum(d["volume"] for d in self.asks)
        
        if best_bid > 0 and best_ask > 0:
            self.mid_price = (best_bid + best_ask) / 2.0
            self.spread = float(best_ask - best_bid)
            
            # Imbalance using top-1 volume
            # Alternatives: use depth totals for VOI?
            # Standard imbalance usually top-1 or total. 
            # Let's stick to top-1 for now as "imbalance", and maybe "depth_imbalance" for total.
            bid_vol = self.bids[0]["volume"]
            ask_vol = self.asks[0]["volume"]
            denom = bid_vol + ask_vol
            self.imbalance = (bid_vol - ask_vol) / denom if denom > 0 else 0.0
        else:
            self.mid_price = 0.0
            self.spread = 0.0
            self.imbalance = 0.0

    def get_stats(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ts": self.exch_ts,
            "mid_price": self.mid_price,
            "spread": self.spread,
            "imbalance": self.imbalance,
            "bid_depth": getattr(self, "bid_depth_total", 0),
            "ask_depth": getattr(self, "ask_depth_total", 0),
            "best_bid": self.bids[0]["price"] if self.bids else 0,
            "best_ask": self.asks[0]["price"] if self.asks else 0,
            "last_price": self.last_price
        }

    def get_snapshot(self) -> Dict[str, Any]:
        """Thread-safe snapshot of LOB state (deep copy)."""
        return {
            "symbol": self.symbol,
            "bids":  [d.copy() for d in self.bids],
            "asks":  [d.copy() for d in self.asks],
            "ts": self.exch_ts,
            "version": self.version,
            "stats": self.get_stats()
        }

class LOBEngine:
    def __init__(self):
        self.books: Dict[str, BookState] = {}
        self._lock = Lock()
        self.metrics = MetricsRegistry.get()

    def get_book(self, symbol: str) -> BookState:
        with self._lock:
            if symbol not in self.books:
                self.books[symbol] = BookState(symbol)
            return self.books[symbol]

    def apply_snapshot(self, snapshot: Dict[str, Any]):
        symbol = snapshot["symbol"]
        book = self.get_book(symbol)
        
        with self._lock:
            book.apply_snapshot(
                snapshot.get("bids", []), 
                snapshot.get("asks", []), 
                snapshot.get("exch_ts", 0)
            )
            self.metrics.lob_snapshots_total.labels(symbol=symbol).inc()

    def get_features(self, symbol: str) -> Dict[str, Any]:
        """API for Feature Consumption."""
        book = self.get_book(symbol)
        # Lockless read might be okay if we accept tearing, but Python GIL helps.
        # But get_stats() creates a new dict, which is safe once created.
        # Accessing `book.mid_price` directly might be partial if updated in thread?
        # Actually `mid_price` is float (atomic in Python).
        # `get_stats` constructs dict. safest to lock if strict consistency needed.
        # `get_book` is locked. `book` object is shared.
        # Let's lock inside `get_stats` or `get_features`?
        with self._lock:
             return book.get_stats()

    def get_book_snapshot(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Safe snapshot for consumers."""
        with self._lock:
            if symbol not in self.books:
                 return None
            return self.books[symbol].get_snapshot()

    def process_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        symbol = event.get("symbol")
        if not symbol: return None
        
        book = self.get_book(symbol)
        with self._lock:
            etype = event.get("type", "")
            if etype == "BidAsk":
                book.update_incremental(
                    event.get("bids", []),
                    event.get("asks", []),
                    event.get("exch_ts", 0)
                )
                self.metrics.lob_updates_total.labels(symbol=symbol, type="BidAsk").inc()
            elif etype == "Tick":
                book.update_tick(
                    event.get("price", 0),
                    event.get("volume", 0),
                    event.get("exch_ts", 0)
                )
                self.metrics.lob_updates_total.labels(symbol=symbol, type="Tick").inc()
            elif etype == "Snapshot":
                book.apply_snapshot(
                    event.get("bids", []),
                    event.get("asks", []),
                    event.get("exch_ts", 0)
                )
                self.metrics.lob_snapshots_total.labels(symbol=symbol).inc()

            return book.get_stats()
