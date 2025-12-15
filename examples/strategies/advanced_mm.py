
from hft_platform.strategy.base import BaseStrategy
from hft_platform.strategy import factors
from hft_platform.contracts.schema import Side, OrderType, TimeInForce

class AdvancedMarketMaker(BaseStrategy):
    """
    Demonstrates usage of Advanced Alpha Features (Phase 5).
    
    Logic:
    1. Calculate Shannon Entropy to detect liquidity buckets.
    2. Calculate Hurst Exponent to identify Regime (Mean Reversion vs Trend).
    3. Use OFI to adjust Mid-Price (Micro-Price).
    
    Signal:
    - If Hurst < 0.5 (Mean Reverting) AND Entropy is Low (Structured Book):
      -> Quote Tight around Micro-Price.
    - If Hurst > 0.5 (Trending):
      -> Widen spreads or follow trend (OFI sign).
    """
    
    def __init__(self, strategy_id: str, **kwargs):
        super().__init__(strategy_id, **kwargs)
        # Initialize Rolling Estimators
        self.roll_estimator = factors.create_roll_estimator(window=20)
        self.amihud_estimator = factors.create_amihud_estimator(window=20)
        
        # State
        self.tick_history = [] # Price history for Hurst
        self.MAX_HISTORY = 100
        
    def on_lob(self, lob: dict, metadata: dict):
        symbol = metadata.get("symbol")
        
        # 1. Calculate Features
        # ---------------------
        
        # A. Entropy (Distribution of Volume)
        # Normalized LOB for calculation
        norm_lob = factors.normalize_lob(lob) 
        entropy = factors.price_entropy(norm_lob)
        
        # B. Micro-Price (OFI adjustment)
        # Assumes OFI is computed by engine and typically passed in stats, 
        # but here we calculate instantaneous Imbalance for Micro-Price demo
        # (Real system has OFI available in 'stats' event, here we compute static I)
        micro_price = factors.stoikov_micro_price(lob)
        
        # C. Effective Spread (Roll)
        mid_price = (lob["bids"][0][0] + lob["asks"][0][0]) / 2.0
        eff_spread = self.roll_estimator.update(mid_price)
        
        # D. Regime (Hurst)
        # Accumulate mid prices
        self.tick_history.append(mid_price)
        if len(self.tick_history) > self.MAX_HISTORY:
            self.tick_history.pop(0)
            
        hurst = 0.5
        if len(self.tick_history) >= 50:
            hurst = factors.get_hurst(self.tick_history)
            
        # 2. Logic / Decision
        # -------------------
        
        self.logger.info("Alpha Signals", 
                         symbol=symbol, 
                         entropy=f"{entropy:.2f}", 
                         hurst=f"{hurst:.2f}", 
                         micro_price=f"{micro_price:.2f}",
                         eff_spread=f"{eff_spread:.4f}")
                         
        # Simple Regime Switch
        if hurst < 0.45:
            # Strong Mean Reversion
            # Quote passive at Micro-Price +/- Spread
            spread = max(1, int(eff_spread * 10000)) if eff_spread > 0 else 5 # Ticks
            
            bid_px = int(micro_price * 10000) - spread
            ask_px = int(micro_price * 10000) + spread
            
            # Place Orders (Demo)
            # Check if we assume 'on_lob' triggers trading (usually on_tick or timer)
            # Here we just log intent
            self.logger.info("Signal: MEAN_REVERSION -> Quote Tight", bid=bid_px, ask=ask_px)
            
            # self.place_limit_order(symbol, Side.BUY, bid_px, 1)
            # self.place_limit_order(symbol, Side.SELL, ask_px, 1)
            
        elif hurst > 0.55:
            # Trending
            # Follow Trend?
            # If price is rising (latest return > 0), buy.
            pass

    def on_trade(self, trade: dict):
        # Update Amihud (Illiquidity)
        ret = 0.0001 # Mock return
        vol = trade.get("volume", 0)
        if vol > 0:
            illiquidity = self.amihud_estimator.update(ret, vol)
            self.logger.info("Liquidity Update", amihud=illiquidity)
