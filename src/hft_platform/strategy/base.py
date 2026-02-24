import os
from abc import ABC
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Union

from structlog import get_logger

# Fill/Order Events might be imported from contracts or events
from hft_platform.contracts.execution import FillEvent, OrderEvent
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.events import BidAskEvent, LOBStatsEvent, TickEvent

logger = get_logger("strategy")

# When HFT_STRICT_PRICE_MODE=1, float prices raise TypeError instead of warning.
# Enables CI enforcement of the Precision Law (no float prices in production).
_STRICT_PRICE = os.getenv("HFT_STRICT_PRICE_MODE", "0") == "1"


class StrategyContext:
    """Read-only context passed to strategy."""

    __slots__ = (
        "positions",
        "strategy_id",
        "_intent_factory",
        "_price_scaler",
        "_lob_source",
        "_lob_l1_source",
    )

    def __init__(
        self,
        positions,
        strategy_id: str,
        intent_factory: Callable[..., OrderIntent],
        price_scaler: Callable[[str, int | Decimal], int],
        lob_source: Callable[[str], Optional[Dict]] | None = None,
        lob_l1_source: Callable[[str], Optional[tuple]] | None = None,
    ):
        self.positions = positions
        self.strategy_id = strategy_id
        self._intent_factory = intent_factory
        self._price_scaler = price_scaler
        self._lob_source = lob_source
        self._lob_l1_source = lob_l1_source

    def place_order(
        self,
        *,
        symbol: str,
        side: Side,
        price: int | Decimal,
        qty: int,
        tif: TIF = TIF.LIMIT,
        intent_type: IntentType = IntentType.NEW,
        target_order_id: Optional[str] = None,
    ) -> OrderIntent:
        # Auto-scaling convenience: int = already scaled, Decimal = needs scaling
        if isinstance(price, int):
            scaled_price = price
        elif isinstance(price, Decimal):
            scaled_price = self.scale_price(symbol, price)
        else:
            # Legacy float support — raise in strict mode, warn otherwise.
            if _STRICT_PRICE:
                raise TypeError(
                    f"Float price rejected (HFT_STRICT_PRICE_MODE=1): "
                    f"symbol={symbol!r} price={price!r} — use int (scaled) or Decimal"
                )
            logger.warning(
                "Float price deprecated - use int (scaled) or Decimal",
                symbol=symbol,
                price=price,
            )
            scaled_price = self.scale_price(symbol, Decimal(str(price)))

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

    def scale_price(self, symbol: str, price: int | Decimal) -> int:
        return self._price_scaler(symbol, price)

    def get_l1_scaled(self, symbol: str) -> Optional[tuple]:
        """Preferred L1 fast path over dict snapshots.

        Returns a tuple:
          (timestamp_ns, best_bid, best_ask, mid_price_x2, spread_scaled, bid_depth, ask_depth)
        All price fields are scaled integers.
        """
        if self._lob_l1_source is None:
            return None
        return self._lob_l1_source(symbol)


class BaseStrategy(ABC):
    """
    High-level Strategy SDK.
    """

    def __init__(self, strategy_id: str, **kwargs):
        self.strategy_id = strategy_id
        self.config = kwargs

        subs = kwargs.get("subscribe_symbols") or kwargs.get("symbols") or []
        self.symbols = set(subs)
        self.enabled = True

        self.ctx: Optional[StrategyContext] = None
        self._generated_intents: List[OrderIntent] = []

    # --- Event Handlers ---

    def on_tick(self, event: TickEvent) -> None:
        """Handle Tick Data."""
        pass

    def on_book_update(self, event: BidAskEvent) -> None:
        """Handle LOB Incremental Updates."""
        pass

    def on_stats(self, event: LOBStatsEvent) -> None:
        """Handle Derived LOB Stats (Mid, Spread)."""
        pass

    def on_fill(self, event: FillEvent) -> None:
        """Handle Fill Reports."""
        pass

    def on_order(self, event: OrderEvent) -> None:
        """Handle Order Status Updates."""
        pass

    # --- Internal Dispatch ---

    def handle_event(
        self, ctx: StrategyContext, event: Union[TickEvent, BidAskEvent, LOBStatsEvent, FillEvent, OrderEvent]
    ) -> List[OrderIntent]:
        self.ctx = ctx
        self._generated_intents.clear()

        # Auto-filter by symbol if applicable
        if hasattr(event, "symbol") and self.symbols:
            if event.symbol not in self.symbols:
                # Special case for Fills/Orders which might target this strategy specifically?
                # Strategy logic usually cares about its own fills regardless of symbol list (e.g. closing legacy)
                # But Runner handles private dispatch.
                # Here we just filter Market Data.
                if not isinstance(event, (FillEvent, OrderEvent)):
                    return []

        if isinstance(event, TickEvent):
            self.on_tick(event)
        elif isinstance(event, BidAskEvent):
            self.on_book_update(event)
        elif isinstance(event, LOBStatsEvent):
            self.on_stats(event)
        elif isinstance(event, FillEvent):
            self.on_fill(event)
        elif isinstance(event, OrderEvent):
            self.on_order(event)

        return self._generated_intents

    # --- Actions ---

    def buy(self, symbol: str, price: int | Decimal, qty: int, tif: TIF = TIF.LIMIT):
        self._place(symbol, Side.BUY, price, qty, tif)

    def sell(self, symbol: str, price: int | Decimal, qty: int, tif: TIF = TIF.LIMIT):
        self._place(symbol, Side.SELL, price, qty, tif)

    def cancel(self, symbol: str, order_id: str):
        if not self.ctx:
            return
        intent = self.ctx.place_order(
            symbol=symbol, side=Side.BUY, price=0, qty=0, intent_type=IntentType.CANCEL, target_order_id=order_id
        )
        self._generated_intents.append(intent)

    def position(self, symbol: str) -> int:
        if not self.ctx or not self.ctx.positions:
            return 0
        return self.ctx.positions.get(symbol, 0)

    def _place(self, symbol, side, price, qty, tif):
        if not self.ctx:
            return
        intent = self.ctx.place_order(
            symbol=symbol, side=side, price=price, qty=qty, tif=tif, intent_type=IntentType.NEW
        )
        self._generated_intents.append(intent)
