import heapq

from structlog import get_logger

from hft_platform.events import BidAskEvent, TickEvent
from hft_platform.strategy.base import BaseStrategy

try:
    from hft_platform.rust_core import AlphaStrategy
except ImportError:
    AlphaStrategy = None

try:
    from hft_platform.observability.metrics import MetricsRegistry as _MetricsRegistry
except Exception as _exc:
    _MetricsRegistry = None  # type: ignore[misc,assignment]

logger = get_logger("rust_alpha")


class Strategy(BaseStrategy):
    """
    Production Adapter for Rust-Based Alpha Strategy.
    Combines:
    1. Deep Imbalance (L4)
    2. Trade Momentum (Spread)
    3. Hawkes Intensity (Risk)
    """

    default_params = {
        "depth_level": 3,
        "hawkes_mu": 0.5,
        "hawkes_alpha": 1.0,
        "hawkes_beta": 20.0,
        "signal_threshold": 0.3,
        "max_pos": 5,
        "lot_size": 1,
        "tick_size": 1.0,
    }

    _PARAM_BOUNDS = {
        "depth_level": (0, 10),
        "hawkes_mu": (0.0, 100.0),
        "hawkes_alpha": (0.0, 100.0),
        "hawkes_beta": (0.1, 1000.0),
        "signal_threshold": (0.0, 10.0),
        "max_pos": (1, 10000),
        "lot_size": (1, 1000),
        "tick_size": (0.0001, 1000.0),
    }

    def __init__(self, strategy_id: str, **params):
        super().__init__(strategy_id, **params)
        self.params = {**self.default_params, **(params or {})}
        self._validate_params()

        if AlphaStrategy is None:
            raise ImportError("hft_platform.rust_core not found. Please build extension.")

        self.core = AlphaStrategy(
            self.params["depth_level"],
            self.params["hawkes_mu"],
            self.params["hawkes_alpha"],
            self.params["hawkes_beta"],
        )

        self.best_bid: int = 0
        self.best_ask: int = 0

        self._entry_price: dict[str, int] = {}

        self._exc_metrics = None
        try:
            if _MetricsRegistry is not None:
                m = _MetricsRegistry.get()
                self._exc_metrics = getattr(m, "strategy_exceptions_total", None)
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            pass

        logger.info("RustAlphaStrategy Initialized", params=self.params)

        self._max_book_levels = 50
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}

    def _validate_params(self) -> None:
        """Validate strategy parameters are within acceptable bounds."""
        errors = []
        for param, (min_val, max_val) in self._PARAM_BOUNDS.items():
            value = self.params.get(param)
            if value is None:
                continue
            if not isinstance(value, (int, float)):
                errors.append(f"{param}: expected numeric, got {type(value).__name__}")
            elif value < min_val or value > max_val:
                errors.append(f"{param}: {value} out of bounds [{min_val}, {max_val}]")

        if errors:
            raise ValueError(f"Invalid strategy parameters: {'; '.join(errors)}")

    def _trim_book(self) -> None:
        """Trim shadow books to max size, keeping best levels.

        Uses heapq.nlargest/nsmallest (O(n)) instead of sorted() (O(n log n)).
        Early-return guard skips all work when within size limits (most common case).
        """
        n_bids, n_asks = len(self.bids), len(self.asks)
        if n_bids <= self._max_book_levels and n_asks <= self._max_book_levels:
            return

        if n_bids > self._max_book_levels:
            keep = set(heapq.nlargest(self._max_book_levels, self.bids))
            for p in list(self.bids):
                if p not in keep:
                    del self.bids[p]

        if n_asks > self._max_book_levels:
            keep = set(heapq.nsmallest(self._max_book_levels, self.asks))
            for p in list(self.asks):
                if p not in keep:
                    del self.asks[p]

    def on_book_update(self, event: BidAskEvent) -> None:
        if event.symbol not in self.symbols:
            return

        try:
            if len(event.bids) > 0:
                for p, q in event.bids:
                    price = int(p)
                    qty = int(q)
                    if qty <= 0:
                        self.bids.pop(price, None)
                    else:
                        self.bids[price] = qty

            if len(event.asks) > 0:
                for p, q in event.asks:
                    price = int(p)
                    qty = int(q)
                    if qty <= 0:
                        self.asks.pop(price, None)
                    else:
                        self.asks[price] = qty

            self._trim_book()

            if self.bids and self.asks:
                self.best_bid = max(self.bids.keys())
                self.best_ask = min(self.asks.keys())

                sorted_bids = heapq.nlargest(5, self.bids.items(), key=lambda x: x[0])
                sorted_asks = heapq.nsmallest(5, self.asks.items(), key=lambda x: x[0])

                signal = self.core.on_depth(sorted_bids, sorted_asks)

                self._execute_on_signal(event.symbol, signal)
        except Exception as e:
            logger.error("Error in on_book_update", error=str(e), symbol=event.symbol)
            if self._exc_metrics:
                try:
                    self._exc_metrics.labels(
                        strategy=self.strategy_id,
                        exception_type=type(e).__name__,
                        method="on_book_update",
                    ).inc()
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    pass

    def on_tick(self, event: TickEvent) -> None:
        if event.symbol not in self.symbols:
            return

        try:
            if (self.best_bid <= 0 or self.best_ask <= 0) and self.ctx:
                l1 = self.ctx.get_l1_scaled(event.symbol)
                if l1 is not None:
                    _ts, best_bid, best_ask, _mid_x2, _spread, _bid_depth, _ask_depth = l1
                    if best_bid > 0:
                        self.best_bid = int(best_bid)
                    if best_ask > 0:
                        self.best_ask = int(best_ask)

            is_buyer_maker = False

            is_aggressor_buy = False
            if self.best_ask > 0 and event.price >= self.best_ask:
                is_aggressor_buy = True
            elif self.best_bid > 0 and event.price <= self.best_bid:
                is_aggressor_buy = False
            else:
                is_aggressor_buy = True

            is_buyer_maker = not is_aggressor_buy

            self.core.on_trade(int(event.meta.source_ts), event.price, event.volume, is_buyer_maker)
        except Exception as e:
            logger.error("Error in on_tick", error=str(e), symbol=event.symbol)
            if self._exc_metrics:
                try:
                    self._exc_metrics.labels(
                        strategy=self.strategy_id,
                        exception_type=type(e).__name__,
                        method="on_tick",
                    ).inc()
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    pass

    def _execute_on_signal(self, symbol: str, signal: float) -> None:
        threshold = self.params["signal_threshold"]
        pos = self.position(symbol)
        max_pos = self.params["max_pos"]
        qty = self.params["lot_size"]

        stop_ticks = int(self.params.get("stop_loss_ticks", 10))
        tick_scaled = int(self.params["tick_size"] * 10_000)
        stop_dist = stop_ticks * tick_scaled
        entry = self._entry_price.get(symbol, 0)

        if pos > 0 and entry > 0 and self.best_bid > 0 and self.best_bid < entry - stop_dist:
            self.sell(symbol, self.best_bid, abs(pos))
            self._entry_price.pop(symbol, None)
            logger.info("stop_loss_triggered", symbol=symbol, side="long", entry=entry, bid=self.best_bid)
            return

        if pos < 0 and entry > 0 and self.best_ask > 0 and self.best_ask > entry + stop_dist:
            self.buy(symbol, self.best_ask, abs(pos))
            self._entry_price.pop(symbol, None)
            logger.info("stop_loss_triggered", symbol=symbol, side="short", entry=entry, ask=self.best_ask)
            return

        if signal > threshold:
            if pos < max_pos:
                price = self.best_bid
                if price > 0:
                    self.buy(symbol, price, qty)
                    self._entry_price[symbol] = price

        elif signal < -threshold:
            if pos > -max_pos:
                price = self.best_ask
                if price > 0:
                    self.sell(symbol, price, qty)
                    self._entry_price[symbol] = price
