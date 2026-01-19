from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy


class SimpleMarketMaker(BaseStrategy):
    """
    Reference implementation for a simple market-making strategy.
    Consumes LOBStatsEvent and places symmetric quotes around fair value.
    """

    def on_stats(self, event: LOBStatsEvent):
        symbol = event.symbol
        mid_price = event.mid_price
        spread = event.spread
        imbalance = event.imbalance

        # 1. Access State
        pos = self.position(symbol)

        # 2. Compute Micro Price (Alpha)
        # Simple linear micro-price: Mid + (Imbalance * Spread * Coeff)
        coeff = 0.2
        micro_price = mid_price + (imbalance * spread * coeff)

        # 3. Inventory Skew (Risk)
        tick_size = 0.5
        skew = -(pos / 5) * tick_size

        fair_value = micro_price + skew

        # 4. Quote Generation
        half_spread = spread * 0.5
        quote_width = max(tick_size, half_spread)

        bid_price = fair_value - quote_width
        ask_price = fair_value + quote_width

        # 5. Execution
        max_pos = 100
        qty = 1

        if pos < max_pos:
            self.buy(symbol, bid_price, qty)

        if pos > -max_pos:
            self.sell(symbol, ask_price, qty)
