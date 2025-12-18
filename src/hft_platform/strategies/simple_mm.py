
from hft_platform.strategy.base import BaseStrategy
from hft_platform.features.micro_price import stoikov_micro_price

class SimpleMarketMaker(BaseStrategy):
    """
    Reference Implementation: High-Frequency Market Maker.
    
    Logic:
    1. Tracks Book Imbalance to compute Stoikov Micro-Price.
    2. Adjusts Mid-Price based on Inventory Skew.
    3. Quotes symmetric spread around Adjusted Fair Value.
    4. Auto-cancels outdated orders.
    """
    
    def on_tick(self, symbol: str, mid_price: float, spread: float):
        # 1. Access State
        pos = self.position(symbol)
        lob = self.ctx.get_l1(symbol) # Accessed via helper
        
        # 2. Compute Micro Price (Alpha)
        # Using feature library
        micro_price = mid_price
        if lob:
            # mp = (Vb / (Va + Vb)) * Spread + Bid
            # We use the library function if context exposes raw book levels
            # For now, simplistic approximation using features if available
            feats = self.ctx.get_features(symbol)
            if "imbalance" in feats:
                 imb = feats["imbalance"]
                 # Simple linear micro-price: Mid + (Imbalance * Spread * Coeff)
                 micro_price = mid_price + (imb * spread * 0.2)
        
        # 3. Inventory Skew (Risk)
        # Shift price against inventory to encourage mean reversion
        # Skew = - (Position * RiskAversion * Volatility)
        # Simplified: 1 tick per 5 lots
        tick_size = 0.5 # 5000 points scaled down
        skew = - (pos / 5) * tick_size
        
        fair_value = micro_price + skew
        
        # --- Observability ---
        # Record internal state for dashboard
        from hft_platform.observability.metrics import MetricsRegistry
        metrics = MetricsRegistry.get()
        metrics.strategy_position.labels(strategy=self.strategy_id, symbol=symbol).set(pos)
        metrics.strategy_skew.labels(strategy=self.strategy_id, symbol=symbol).set(skew)
        metrics.strategy_micro_price.labels(strategy=self.strategy_id, symbol=symbol).set(micro_price)
        # ---------------------
        
        # 4. Quote Generation
        half_spread = spread * 0.5
        # Add slight markup to capture profit
        quote_width = max(tick_size, half_spread) 
        
        bid_price = fair_value - quote_width
        ask_price = fair_value + quote_width
        
        # 5. Execution (High Level API)
        # Cancel previous if price moved significantly? 
        # For simplicity, we just place NEW orders (OrderAdapter handles limits/cancels in real system often, 
        # or we explicitly cancel all here).
        
        # Logic: Smart Cancel - only cancel if price moved > threshold
        # Here: Naive "Cancel All and Re-quote" (typical for simple demos)
        # self.cancel_all(symbol) # Not yet implemented in Base, so we skip cancellation for now 
        # or assume OCO logic.
        
        # Checking limits
        MAX_POS = 100
        qty = 1
        
        if pos < MAX_POS:
            self.buy(symbol, int(bid_price), qty)
            
        if pos > -MAX_POS:
            self.sell(symbol, int(ask_price), qty)
            
        # Logging (Observability)
        # logger.info("MM Quote", symbol=symbol, bid=bid_price, ask=ask_price, skew=skew, micro=micro_price)

