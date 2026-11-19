from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy


class SimpleMarketMaker(BaseStrategy):
    """
    Reference implementation for a simple market-making strategy.
    Consumes LOBStatsEvent and places symmetric quotes around fair value.

    Precision Law: All price calculations use scaled integers.
    """

    IMBALANCE_COEFF_PERCENT: int = 20
    INVENTORY_SKEW_DIVISOR: int = 5

    def __init__(self, strategy_id: str, **kwargs):
        super().__init__(strategy_id, **kwargs)
        self._tick_size_ratio_pct: int = kwargs.get("tick_size_ratio_pct", 50)
        self._max_pos: int = kwargs.get("max_pos", 100)
        self._stop_loss_scaled: int = kwargs.get("stop_loss_scaled", 0)
        self._inventory_skew_divisor: int = kwargs.get("inventory_skew_divisor", self.INVENTORY_SKEW_DIVISOR)

    def on_stats(self, event: LOBStatsEvent):
        symbol = event.symbol

        mid_price_x2 = event.mid_price_x2
        spread_scaled = event.spread_scaled
        imbalance = event.imbalance

        if mid_price_x2 is None or spread_scaled is None:
            return
        if mid_price_x2 <= 0 or spread_scaled <= 0 or event.best_bid <= 0 or event.best_ask <= 0:
            return

        pos = self.position(symbol)

        imbalance_adj = int(imbalance * spread_scaled * self.IMBALANCE_COEFF_PERCENT * 2 // 100)
        micro_price_x2 = mid_price_x2 + imbalance_adj

        tick_size_scaled = max(1, spread_scaled * self._tick_size_ratio_pct // 100)
        skew_x2 = -(pos * tick_size_scaled * 2) // self._inventory_skew_divisor

        fair_value_x2 = micro_price_x2 + skew_x2

        half_spread_scaled = max(1, spread_scaled // 2)
        quote_width_scaled = max(tick_size_scaled, half_spread_scaled)

        bid_price_scaled = (fair_value_x2 - quote_width_scaled * 2) // 2
        ask_price_scaled = (fair_value_x2 + quote_width_scaled * 2) // 2

        if self._stop_loss_scaled > 0 and pos != 0:
            mid_scaled = mid_price_x2 // 2
            entry_approx = getattr(self, "_last_entry_price_scaled", mid_scaled)
            unrealized = (mid_scaled - entry_approx) * pos
            if unrealized < -self._stop_loss_scaled:
                if pos > 0:
                    self.sell(symbol, bid_price_scaled, abs(pos))
                else:
                    self.buy(symbol, ask_price_scaled, abs(pos))
                return

        max_pos = self._max_pos
        qty = 1

        if pos < max_pos:
            self.buy(symbol, bid_price_scaled, qty)
            if pos == 0:
                self._last_entry_price_scaled = bid_price_scaled

        if pos > -max_pos:
            self.sell(symbol, ask_price_scaled, qty)
            if pos == 0:
                self._last_entry_price_scaled = ask_price_scaled
