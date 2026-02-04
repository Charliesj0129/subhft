

from structlog import get_logger

from hft_platform.events import BidAskEvent, TickEvent
from hft_platform.strategy.base import BaseStrategy

# Import Rust Core
# We use try/except to allow running in envs where it's not built yet,
# but for production it should fail if missing.
try:
    from hft_platform.rust_core import AlphaStrategy
except ImportError:
    AlphaStrategy = None

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
        "depth_level": 3, # 0-indexed L4 (Level 3)
        "hawkes_mu": 0.5,
        "hawkes_alpha": 1.0,
        "hawkes_beta": 20.0,
        "signal_threshold": 0.3,
        "max_pos": 5,
        "lot_size": 1,
        "tick_size": 1.0,
    }

    def __init__(self, strategy_id: str, **params):
        super().__init__(strategy_id, **params)
        self.params = {**self.default_params, **(params or {})}

        if AlphaStrategy is None:
            raise ImportError("hft_platform.rust_core not found. Please build extension.")

        # Initialize Rust Core
        self.core = AlphaStrategy(
            self.params["depth_level"],
            self.params["hawkes_mu"],
            self.params["hawkes_alpha"],
            self.params["hawkes_beta"]
        )

        self.best_bid = 0.0
        self.best_ask = 0.0

        logger.info("RustAlphaStrategy Initialized", params=self.params)

        # Shadow Book State
        self.bids = {}
        self.asks = {}

    def on_book_update(self, event: BidAskEvent) -> None:
        if event.symbol not in self.symbols:
            return

        # Update Shadow Book
        # Bids
        if len(event.bids) > 0:
            for p, q in event.bids:
                if q <= 0:
                    self.bids.pop(p, None)
                else:
                    self.bids[p] = q

        # Asks
        if len(event.asks) > 0:
            for p, q in event.asks:
                if q <= 0:
                    self.asks.pop(p, None)
                else:
                    self.asks[p] = q

        # Update BBO for Trade Direction Inference
        if self.bids and self.asks:
            self.best_bid = max(self.bids.keys())
            self.best_ask = min(self.asks.keys())

            # Sampling: Only compute every X updates?
            # For now, compute every update to verify responsiveness.

            # Convert to Sorted List for Rust
            # Descending for Bids, Ascending for Asks
            sorted_bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:5]
            sorted_asks = sorted(self.asks.items(), key=lambda x: x[0])[:5]

            # Call Rust Core
            signal = self.core.on_depth(sorted_bids, sorted_asks)

            # Execution
            self._execute_on_signal(event.symbol, signal)

    def on_tick(self, event: TickEvent) -> None:
        if event.symbol not in self.symbols:
            return

        # Infer direction
        is_buyer_maker = False # Logic depends on Aggressor.
        # If Price >= Ask, Aggressor is Buyer -> Maker is Seller.
        # If Price <= Bid, Aggressor is Seller -> Maker is Buyer.
        # Rust `on_trade` expects `is_buyer_maker` (boolean).
        # Actually `on_trade` in Rust logic (Momentum) uses it to decaying impact?
        # Let's assume standard:
        # Aggressor Buy -> Price Up.
        # We pass `is_buyer_maker`?
        # Let's check `strategy.rs` usage or just pass `is_buyer` (Aggressor).
        # Rust signature: `on_trade(ts, px, qty, is_buyer_maker)`

        # Check `run_rust_strategy.py`: `is_buyer_maker = (side == -1)` (Sell Aggressor).
        # So `is_buyer_maker` means "Is the Maker a Buyer?" i.e. "Is Aggressor a Seller?"

        is_aggressor_buy = False
        if self.best_ask > 0 and event.price >= self.best_ask:
            is_aggressor_buy = True
        elif self.best_bid > 0 and event.price <= self.best_bid:
            is_aggressor_buy = False
        else:
            # Unclear/Mid, assume Buy if close to Ask?
            is_aggressor_buy = True

        # is_buyer_maker = True if Aggressor is Sell
        is_buyer_maker = not is_aggressor_buy

        self.core.on_trade(int(event.meta.source_ts), float(event.price), float(event.volume), is_buyer_maker)

    def _execute_on_signal(self, symbol, signal):
        threshold = self.params["signal_threshold"]
        pos = self.position(symbol)
        max_pos = self.params["max_pos"]
        qty = self.params["lot_size"]

        # Signal > Thresh -> Buy
        if signal > threshold:
            if pos < max_pos:
                # Join Bid
                price = self.best_bid
                if price > 0:
                    self.buy(symbol, price, qty)

        # Signal < -Thresh -> Sell
        elif signal < -threshold:
            if pos > -max_pos:
                # Join Ask
                price = self.best_ask
                if price > 0:
                    self.sell(symbol, price, qty)
