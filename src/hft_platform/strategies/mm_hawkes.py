import numpy as np
from numba import njit, float64, int64, uint64
from numba.experimental import jitclass
from hftbacktest import HashMapMarketDepthBacktest, GTX, LIMIT, BUY, SELL

# --- Signal Trackers (Embedded for Standalone) ---

K_PROPAGATOR = 3

@jitclass([
    ('mu', float64),
    ('alpha', float64),
    ('beta', float64),
    ('intensity', float64),
    ('last_ts', int64)
])
class HawkesTracker:
    def __init__(self, mu, alpha, beta):
        self.mu = mu
        self.alpha = alpha
        self.beta = beta
        self.intensity = mu
        self.last_ts = 0

    def update(self, current_ts, is_event):
        if self.last_ts == 0:
            self.last_ts = current_ts
            return
        dt_ns = current_ts - self.last_ts
        if dt_ns > 0:
            dt_sec = float(dt_ns) * 1e-9
            decay = np.exp(-self.beta * dt_sec)
            self.intensity = self.mu + (self.intensity - self.mu) * decay
            if is_event:
                self.intensity += self.alpha
            self.last_ts = current_ts

@jitclass([
    ('weights', float64[:]),
    ('betas', float64[:]),
    ('components', float64[:]),
    ('last_ts', int64),
    ('total_impact', float64)
])
class PropagatorTracker:
    def __init__(self):
        self.weights = np.array([0.5, 0.3, 0.2], dtype=np.float64)
        self.betas = np.array([100.0, 10.0, 1.0], dtype=np.float64)
        self.components = np.zeros(K_PROPAGATOR, dtype=np.float64)
        self.last_ts = 0
        self.total_impact = 0.0

    def update(self, current_ts):
        if self.last_ts == 0:
            self.last_ts = current_ts
            return
        dt_ns = current_ts - self.last_ts
        if dt_ns > 0:
            dt_sec = float(dt_ns) * 1e-9
            for k in range(K_PROPAGATOR):
                self.components[k] *= np.exp(-self.betas[k] * dt_sec)
            self.last_ts = current_ts
            self._recalc()

    def add_event(self, sign, qty):
        impact = sign * np.log(1.0 + qty)
        for k in range(K_PROPAGATOR):
            self.components[k] += self.weights[k] * impact
        self._recalc()

    def _recalc(self):
        s = 0.0
        for k in range(K_PROPAGATOR):
            s += self.components[k]
        self.total_impact = s

# --- Market Maker Strategy ---

@njit
def strategy(hbt):
    asset_no = 0

    # --- Parameters ---
    base_spread_ticks = 2.0
    hawkes_spread_coeff = 0.5
    propagator_skew_coeff = 0.5
    risk_aversion = 0.1
    order_qty = 1.0
    requote_interval_ns = 100_000_000 # 100ms

    # --- State ---
    hawkes = HawkesTracker(1.0, 0.5, 10.0)
    propagator = PropagatorTracker()
    inventory = 0.0
    next_order_id = 1
    last_quote_ts = 0
    bid_order_id = 0
    ask_order_id = 0

    # --- Main Loop ---
    while hbt.elapse(1_000_000) == 0: # 1ms
        current_ts = hbt.current_timestamp
        depth = hbt.depth(asset_no)
        
        if depth.best_bid == 0.0 or depth.best_ask == 0.0:
            continue

        tick_size = depth.tick_size
        mid_price = (depth.best_bid + depth.best_ask) / 2.0

        # 1. Update Signals
        trades = hbt.last_trades(asset_no)
        is_event = len(trades) > 0
        hawkes.update(current_ts, is_event)
        propagator.update(current_ts)
        
        for i in range(len(trades)):
            trade = trades[i]
            sign = float(trade.ival)
            qty = float(trade.qty)
            propagator.add_event(sign, qty)
        hbt.clear_last_trades(asset_no)

        # 2. Calculate Quoting Prices
        # Spread: widens with Hawkes intensity
        spread = base_spread_ticks * tick_size * (1.0 + hawkes_spread_coeff * hawkes.intensity)
        half_spread = spread / 2.0

        # Skew: adjust reservation price based on inventory and propagator
        inv_penalty = risk_aversion * inventory * tick_size
        prop_skew = propagator_skew_coeff * propagator.total_impact * tick_size
        
        reservation_price = mid_price - inv_penalty + prop_skew

        bid_price = round((reservation_price - half_spread) / tick_size) * tick_size
        ask_price = round((reservation_price + half_spread) / tick_size) * tick_size

        # 3. Order Management (Cancel & Requote)
        # Simple logic: requote every N ms
        if current_ts - last_quote_ts > requote_interval_ns:
            # Cancel old orders
            if bid_order_id != 0:
                hbt.cancel(asset_no, bid_order_id, False)
            if ask_order_id != 0:
                hbt.cancel(asset_no, ask_order_id, False)
            
            hbt.clear_inactive_orders(asset_no)

            # Place new quotes
            bid_order_id = next_order_id
            next_order_id += 1
            hbt.submit_buy_order(asset_no, bid_order_id, bid_price, order_qty, GTX, LIMIT, False)

            ask_order_id = next_order_id
            next_order_id += 1
            hbt.submit_sell_order(asset_no, ask_order_id, ask_price, order_qty, GTX, LIMIT, False)
            
            last_quote_ts = current_ts

        # 4. Track Fills (Inventory Update) - Simplified
        # In hftbacktest, you'd check order status. For simplicity, we just proceed.
        # A proper implementation would check hbt.orders() for fill status.

    return True
