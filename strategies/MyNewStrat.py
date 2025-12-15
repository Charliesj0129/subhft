from hft_platform.strategy.base import BaseStrategy

class Mynewstrat(BaseStrategy):
    """
    MyNewStrat Strategy.
    Configured via config.yaml (symbols, params).
    """
    def on_tick(self, symbol: str, mid: float, spread: float):
        # Your Alpha Logic Here
        # Example:
        # if spread > 1.0:
        #     self.buy(symbol, mid - 1, 1)
        pass
