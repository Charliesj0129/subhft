from typing import List, Optional, Callable
from abc import ABC, abstractmethod
from structlog import get_logger

from hft_platform.contracts.strategy import OrderIntent, IntentType, Side, TIF

logger = get_logger("strategy")

class StrategyContext:
    """Read-only context passed to strategy."""
    __slots__ = (
        "lob",
        "positions",
        "storm_guard_state",
        "features",
        "strategy_id",
        "_intent_factory",
        "_price_scaler",
    )

    def __init__(
        self,
        lob,
        positions,
        storm_guard_state,
        strategy_id: str,
        intent_factory: Callable[..., OrderIntent],
        price_scaler: Callable[[str, float], int],
        features=None,
    ):
        self.lob = lob
        self.positions = positions
        self.storm_guard_state = storm_guard_state
        self.features = features or {}
        self.strategy_id = strategy_id
        self._intent_factory = intent_factory
        self._price_scaler = price_scaler

    def get_features(self, symbol: str) -> dict:
        """Access derived market features (mid, spread, imbalance)."""
        return self.features.get(symbol, {})

    def scale_price(self, symbol: str, price: float) -> int:
        return self._price_scaler(symbol, price)

    def place_order(
        self,
        *,
        symbol: str,
        side: Side,
        price: float,
        qty: int,
        tif: TIF = TIF.LIMIT,
        intent_type: IntentType = IntentType.NEW,
        target_order_id: Optional[str] = None,
    ) -> OrderIntent:
        """Helper to build OrderIntent with automatic price scaling."""
        if isinstance(price, int):
            scaled_price = price
        else:
            scaled_price = self.scale_price(symbol, price)

        return self._intent_factory(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side=side,
            price=scaled_price,
            qty=qty,
            tif=tif,
            intent_type=intent_type,
            target_order_id=target_order_id,
        )

class BaseStrategy(ABC):
    """
    High-level Strategy SDK.
    Users should inherit from this class and implement `on_tick` or `on_event`.
    """
    def __init__(self, strategy_id: str, **kwargs):
        self.strategy_id = strategy_id
        # Strategy config
        self.config = kwargs
        
        # Parse subscriptions
        # Can come from kwargs['subscribe_symbols'] or kwargs['symbols'] or config['symbols']
        subs = kwargs.get("subscribe_symbols") or kwargs.get("symbols") or []
        self.symbols = set(subs)
        
        self.enabled = True
        
        
        # Runtime context (set per event)
        self.ctx: Optional[StrategyContext] = None
        self._generated_intents: List[OrderIntent] = []
        
        # State Tracking
        self._open_orders: dict = {} # Map order_id -> intent
        
        # Factors Library
        import hft_platform.strategy.factors
        self.factors = hft_platform.strategy.factors

    # --- Lifecycle Callbacks ---
    
    def on_event(self, event: dict):
        """Override to handle general events."""
        pass
        
    def on_tick(self, symbol: str, mid_price: float, spread: float):
        """Optional convenience callback for tick data."""
        pass
        
    def on_fill(self, event: dict):
        """Callback for Fill events."""
        pass
        
    def on_ack(self, event: dict):
        """Callback for Order Acknowledgement."""
        pass
        
    def on_reject(self, event: dict):
        """Callback for Order Rejection."""
        pass
        
    def on_cancel(self, event: dict):
        """Callback for Order Cancellation confirmation."""
        pass

    def on_book(self, ctx: StrategyContext, event: dict) -> List[OrderIntent]:
        """Internal entry point called by Runner."""
        self.ctx = ctx
        self._generated_intents.clear()
        
        symbol = event.get("symbol")
        # Auto-filter
        if self.symbols and symbol not in self.symbols:
            return []

        # Dispatch based on topic/type
        # Simple heuristic dispatch
        if event.get("topic") == "deal" or event.get("type") == "fill":
            self.on_fill(event)
        elif event.get("type") == "ack":
            self.on_ack(event)
        elif event.get("type") == "reject":
            self.on_reject(event)
        elif event.get("type") == "canceled":
            self.on_cancel(event)
        else:
            # Default to generic event handler
            self.on_event(event)
            # If event has price features, call on_tick
            if "mid_price" in event and symbol:
                 self.on_tick(symbol, event["mid_price"], event.get("spread", 0))

        return self._generated_intents

    # --- High Level Actions ---

    def buy(self, symbol: str, price: float, qty: int, tif: TIF = TIF.LIMIT, strict_price=False):
        """Place a Buy order."""
        self._place(symbol, Side.BUY, price, qty, tif, strict_price)

    def sell(self, symbol: str, price: float, qty: int, tif: TIF = TIF.LIMIT, strict_price=False):
        """Place a Sell order."""
        self._place(symbol, Side.SELL, price, qty, tif, strict_price)

    def cancel(self, symbol: str, order_id: str):
        """Cancel a specific order."""
        # Need target_order_id in intent
        if not self.ctx: return # Safety
        
        intent = self.ctx.place_order(
             symbol=symbol,
             side=Side.BUY, # Dummy
             price=0,
             qty=0,
             intent_type=IntentType.CANCEL,
             target_order_id=order_id
        )
        self._generated_intents.append(intent)

    def _place(self, symbol, side, price, qty, tif, strict_price):
        # Handle auto-scaling? 
        # Context.place_order handles scaling if price is float
        intent = self.ctx.place_order(
            symbol=symbol,
            side=side,
            price=price,
            qty=qty,
            tif=tif,
            intent_type=IntentType.NEW
        )
        self._generated_intents.append(intent)

    # --- State Accessors ---
    
    def mid_price(self, symbol: str) -> float:
        return self.ctx.get_features(symbol).get("mid_price", 0.0)
    
    def spread(self, symbol: str) -> float:
        return self.ctx.get_features(symbol).get("spread", 0.0)

    def position(self, symbol: str) -> int:
        return self.ctx.positions.get(symbol, 0)

