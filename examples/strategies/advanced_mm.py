from typing import List, Optional

from structlog import get_logger

from hft_platform.events import BidAskEvent, TickEvent
from hft_platform.strategy import factors
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("strategy.advanced_mm")


class AdvancedMarketMaker(BaseStrategy):
    """
    Demonstrates usage of advanced alpha features.

    Logic:
    1. Calculate entropy to detect liquidity structure.
    2. Calculate Hurst exponent to estimate regime.
    3. Use micro-price and Roll spread to shape quotes.
    """

    def __init__(self, strategy_id: str, **kwargs):
        super().__init__(strategy_id, **kwargs)
        self.roll_estimator = factors.create_roll_estimator(window=20)
        self.amihud_estimator = factors.create_amihud_estimator(window=20)
        self.mid_history: List[float] = []
        self.max_history = 100
        self.last_trade_price: Optional[int] = None

    def on_book_update(self, event: BidAskEvent) -> None:
        lob = {
            "bids": event.bids.tolist() if hasattr(event.bids, "tolist") else event.bids,
            "asks": event.asks.tolist() if hasattr(event.asks, "tolist") else event.asks,
        }
        if not lob["bids"] or not lob["asks"]:
            return

        entropy = factors.price_entropy(lob)
        micro_price = factors.micro_price(lob)
        mid_price = (lob["bids"][0][0] + lob["asks"][0][0]) / 2.0
        eff_spread = self.roll_estimator.update(mid_price)

        self.mid_history.append(mid_price)
        if len(self.mid_history) > self.max_history:
            self.mid_history.pop(0)

        hurst = 0.5
        if len(self.mid_history) >= 50:
            hurst = factors.get_hurst(self.mid_history)

        logger.info(
            "Alpha Signals",
            symbol=event.symbol,
            entropy=round(entropy, 2),
            hurst=round(hurst, 2),
            micro_price=round(micro_price, 2),
            eff_spread=round(eff_spread, 4),
        )

        # Example quoting logic (prices are already scaled ints)
        spread = max(1, int(eff_spread)) if eff_spread > 0 else 5
        bid_px = int(micro_price) - spread
        ask_px = int(micro_price) + spread

        if hurst < 0.45:
            self.buy(event.symbol, bid_px, 1)
            self.sell(event.symbol, ask_px, 1)
        elif hurst > 0.55:
            # Example: widen quotes or follow trend (no-op)
            pass

    def on_tick(self, event: TickEvent) -> None:
        if self.last_trade_price is None:
            self.last_trade_price = event.price
            return

        ret = (event.price - self.last_trade_price) / max(self.last_trade_price, 1)
        self.last_trade_price = event.price

        amihud = self.amihud_estimator.update(ret, event.price, event.volume)
        logger.info("Liquidity Update", symbol=event.symbol, amihud=amihud)
