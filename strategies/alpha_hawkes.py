import numpy as np
from numba import njit, float64, int64
from numba.experimental import jitclass
from hftbacktest import HashMapMarketDepthBacktest

@jitclass([
    ('mu', float64),
    ('alpha', float64),
    ('beta', float64),
    ('last_ts', int64),
    ('intensity', float64)
])
class HawkesTracker:
    def __init__(self, mu, alpha, beta):
        self.mu = mu
        self.alpha = alpha
        self.beta = beta
        self.last_ts = 0
        self.intensity = mu

    def update(self, current_ts, is_event):
        # Calculate time delta in nanoseconds
        if self.last_ts == 0:
            self.last_ts = current_ts
            return

        dt_ns = current_ts - self.last_ts
        
        # Avoid extremely large dt jumps (start of day or gaps)
        if dt_ns < 0:
            dt_ns = 0
            
        # Convert beta to ns scale? 
        # If input beta is in inverse-seconds, we need to convert dt to seconds or beta to ns.
        # Let's assume beta is provided in inverse-nanoseconds for pure raw calculation,
        # OR we convert dt to seconds.
        # Standard: exp(-beta * dt). 
        # If beta is ~10 (decay in 0.1s), then beta * dt_sec must be ~1.
        # dt_sec = dt_ns * 1e-9.
        # exponent = -beta * dt_ns * 1e-9.
        
        exponent = -self.beta * float(dt_ns) * 1e-9
        decay = np.exp(exponent)
        
        # Recursive update
        self.intensity = self.mu + (self.intensity - self.mu) * decay
        
        if is_event:
            self.intensity += self.alpha
            
        self.last_ts = current_ts

@njit
def hawkes_strategy(hbt):
    asset_no = 0
    
    # Parameters for Hawkes (Example values)
    # mu = 1.0 event/sec
    # alpha = 0.5 (Jump size)
    # beta = 10.0 (Decay speed, 1/0.1s)
    tracker = HawkesTracker(1.0, 0.5, 10.0)
    
    # State tracking for trade counting
    last_trade_qty = 0.0
    
    # Check every 1ms
    while hbt.elapse(1_000_000) == 0:
        current_ts = hbt.current_timestamp
        
        # Access Last Trades to detect events
        # We need to know if a NEW trade occurred since last check.
        # hftbacktest 'last_trades' gives trades in the last batch? Or buffer?
        # Typically we clear them or check against a persistent ID/timestamp.
        # 'hbt.clear_last_trades(asset_no)' is robust if we check every loop.
        
        trades = hbt.last_trades(asset_no)
        is_trade_event = False
        
        if len(trades) > 0:
            is_trade_event = True
            # Optional: Add alpha based on volume? 
            # tracker.update(current_ts, True) for each trade?
            # Or just once per batch?
            # For HFT, every trade counts.
            
            # Note: trades is an array of events.
            # We should arguably process each trade timestamp to be precise,
            # but inside 'elapse', time is fixed to 'current_timestamp' roughly?
            # Actually hbt.last_trades might contain trades with different timestamps if multiple happened.
            # For simplicity in this 'elapse' based loop, we treat the batch as 'occurring now'.
            # A more precise way requires 'wait_next_feed'.
        
        tracker.update(current_ts, is_trade_event)
        
        # Clear trades so we don't double count next loop
        hbt.clear_last_trades(asset_no)
        
        # Trading logic based on intensity...
        if tracker.intensity > 5.0:
            # High activity -> maybe avoid taking liquidity or widen spread
            pass
            
    return True

strategy = hawkes_strategy
