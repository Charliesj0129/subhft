from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy


class SimpleMarketMaker(BaseStrategy):
    """
    Reference implementation for a simple market-making strategy.
    Consumes LOBStatsEvent and places symmetric quotes around fair value.

    Precision Law: All price calculations use scaled integers.
    """

    # Configuration
    IMBALANCE_COEFF_PERCENT: int = 20  # 0.2 as 20%
    INVENTORY_SKEW_DIVISOR: int = 5

    def __init__(self, strategy_id: str, **kwargs):
        super().__init__(strategy_id, **kwargs)
        # Tick size ratio: 50% of spread minimum (configurable)
        self._tick_size_ratio_pct: int = kwargs.get("tick_size_ratio_pct", 50)

    def on_stats(self, event: LOBStatsEvent):
        symbol = event.symbol

        # Use integer fields (Precision Law compliant)
        mid_price_x2 = event.mid_price_x2
        spread_scaled = event.spread_scaled
        imbalance = event.imbalance

        # Guard clause - return early if any values are None or invalid
        if mid_price_x2 is None or spread_scaled is None:
            return
        if mid_price_x2 <= 0 or spread_scaled <= 0 or event.best_bid <= 0 or event.best_ask <= 0:
            return

        # 1. Access State
        pos = self.position(symbol)

        # 2. Compute Micro Price (Alpha) - all in scaled integer units
        # mid_price_x2 is (best_bid + best_ask), so mid = mid_price_x2 // 2
        # micro_price = mid + (imbalance * spread * 0.2)
        # Using integer math: micro_price_x2 = mid_price_x2 + int(imbalance * spread_scaled * 0.4)
        # Since imbalance is [-1, 1] float (bounded ratio), this is acceptable
        imbalance_adj = int(imbalance * spread_scaled * self.IMBALANCE_COEFF_PERCENT * 2 // 100)
        micro_price_x2 = mid_price_x2 + imbalance_adj

        # 3. Inventory Skew (Risk) - scaled integer
        # tick_size is derived from spread (adaptive to instrument)
        tick_size_scaled = max(1, spread_scaled * self._tick_size_ratio_pct // 100)
        # skew = -(pos / 5) * tick_size -> in scaled units
        skew_x2 = -(pos * tick_size_scaled * 2) // self.INVENTORY_SKEW_DIVISOR

        fair_value_x2 = micro_price_x2 + skew_x2

        # 4. Quote Generation - scaled integer
        # half_spread = spread / 2 -> spread_scaled // 2
        # quote_width = max(tick_size, half_spread)
        half_spread_scaled = max(1, spread_scaled // 2)
        quote_width_scaled = max(tick_size_scaled, half_spread_scaled)

        # bid_price = fair_value - quote_width
        # ask_price = fair_value + quote_width
        # Since fair_value_x2 is x2, we need to be consistent
        bid_price_scaled = (fair_value_x2 - quote_width_scaled * 2) // 2
        ask_price_scaled = (fair_value_x2 + quote_width_scaled * 2) // 2

        # 5. Execution - prices are already scaled integers
        max_pos = 100
        qty = 1

        if pos < max_pos:
            self.buy(symbol, bid_price_scaled, qty)

        if pos > -max_pos:
            self.sell(symbol, ask_price_scaled, qty)
