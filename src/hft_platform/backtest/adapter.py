
from typing import Dict, List, Any
import numpy as np
from structlog import get_logger

try:
    from hftbacktest import (
        BacktestAsset,
        HashMapMarketDepthBacktest,
        OrderBus, 
        LinearAsset,
        ConstantLatency,
        PowerProbQueueModel,
        Stat
    )
    from hftbacktest.reader import NpzMarketDepthLoader
    from hftbacktest.order import (
        Buy, Sell, Limit, Market, IOC, ROD
    )
    HFTBACKTEST_AVAILABLE = True
except ImportError:
    HFTBACKTEST_AVAILABLE = False

from hft_platform.contracts.strategy import Side, IntentType, TIF
from hft_platform.strategy.base import BaseStrategy, StrategyContext

logger = get_logger("hbt_adapter")

class HftBacktestAdapter:
    """
    Runs a BaseStrategy instance inside HftBacktest engine.
    """
    def __init__(self, strategy: BaseStrategy, asset_symbol: str, data_path: str, latency_us=100):
        if not HFTBACKTEST_AVAILABLE:
            raise ImportError("hftbacktest not installed")
            
        self.strategy = strategy
        self.symbol = asset_symbol
        self.data_path = data_path
        
        # Setup HftBacktest
        # 1. Asset
        self.asset = LinearAsset(1.0) # Tick size 1.0 or whatever
        
        # 2. Latency Model
        self.latency = ConstantLatency(latency_us * 1000) # ns
        
        # 3. Queue Model
        self.queue_model = PowerProbQueueModel(3.0) # Standard assumption
        
        # 4. Engine
        self.hbt = HashMapMarketDepthBacktest([
            BacktestAsset()
                .data([data_path])
                .linear_asset(1.0)
                .constant_latency(latency_us * 1000, latency_us * 1000)
                .power_prob_queue_model(3.0)
                .int_order_id_converter()
        ])
        
        # Context Mapping
        self.ctx = StubContext(self.hbt, self.symbol)
        
    def run(self):
        logger.info("Starting HftBacktest simulation...")
        
        # Initialize Strategy
        # Strategy expects on_book(ctx, event)
        # We need to bridge the loop.
        
        # HftBacktest loop
        while self.hbt.run():
            if not self.hbt.elapse(1): # Granularity?
                continue
                
            # Current LOB State
            # We construct a mock event for the strategy
            # Efficiently accessing hbt depth
            
            mid = self.get_mid_price()
            if np.isnan(mid):
                continue
                
            event = {
                "symbol": self.symbol,
                "mid_price": mid,
                "spread": self.get_spread(),
                "timestamp": self.hbt.current_timestamp
            }
            
            # Update Context State
            self.ctx.sync_state()
            
            # Call Strategy
            intents = self.strategy.on_book(self.ctx, event)
            
            # Execute Intents
            for intent in intents:
                self.execute_intent(intent)
                
        return self.hbt.close()

    def get_mid_price(self):
        # Access hbt LOB
        dp = self.hbt.depth(0) # asset 0
        bid = dp.best_bid
        ask = dp.best_ask
        if bid == 0 or ask == 2147483647: # Max int check
             return float('nan')
        return (bid + ask) / 2.0

    def get_spread(self):
        dp = self.hbt.depth(0)
        return dp.best_ask - dp.best_bid

    def execute_intent(self, intent):
        # Convert Intent -> HftBacktest Order
        # hbt.submit_buy_order(asset_id, order_id, price, qty, time_in_force, exec_type)
        
        asset_id = 0
        order_id = intent.intent_id 
        price = intent.price
        qty = intent.qty
        tif = ROD if intent.tif == TIF.LIMIT else IOC # Mapping
        
        if intent.intent_type == IntentType.NEW:
            if intent.side == Side.BUY:
                self.hbt.submit_buy_order(asset_id, order_id, price, qty, tif, Limit)
            else:
                self.hbt.submit_sell_order(asset_id, order_id, price, qty, tif, Limit)
        
        elif intent.intent_type == IntentType.CANCEL:
            self.hbt.cancel(asset_id, int(intent.target_order_id))


class StubContext(StrategyContext):
    """
    Mocks StrategyContext but writes to HftBacktest.
    The Strategy.base uses place_order which returns Intent.
    We just need to ensure getters work.
    """
    def __init__(self, hbt, symbol):
        self.hbt = hbt
        self.symbol = symbol
        self.positions = {symbol: 0}
        self.features = {symbol: {}}
        
        # Base constructor needs call but we override behaviors
        # We don't call super().__init__ to avoid tight coupling if not needed
        # Just satisfying protocol
        
    def get_features(self, symbol):
        return self.features.get(symbol, {})
        
    def place_order(self, **kwargs):
        # Use Standard Intent factory logic
        # Re-import to avoid circular
        from hft_platform.contracts.strategy import OrderIntent
        # Generate ID
        import time
        iid = int(time.time_ns() % 1000000) 
        
        return OrderIntent(intent_id=iid, **kwargs)
        
    def sync_state(self):
        # Sync Position
        try:
             # hbt.position(asset_id)
             self.positions[self.symbol] = self.hbt.position(0)
        except: pass
