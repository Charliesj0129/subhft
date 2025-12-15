from structlog import get_logger
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("simple_strat")

class SimpleStrategy(BaseStrategy):
    """
    Example of the new simplified Strategy SDK.
    Lines of code: ~15 (excluding whitespace)
    """
    def __init__(self, strategy_id: str):
        # Subscribe to TSMC (2330)
        super().__init__(strategy_id, subscribe_symbols=["2330"])
        
    def on_tick(self, symbol: str, mid: float, spread: float):
        # Simple Logic: If spread is profitable, quote inside
        
        # High-level state access
        pos = self.position(symbol)
        
        if pos == 0 and spread > 1.0:
            # High-level action
            logger.info("Opportunity found", mid=mid, spread=spread)
            
            # Place buy at mid - 1
            self.buy(symbol, price=mid-1, qty=1)
