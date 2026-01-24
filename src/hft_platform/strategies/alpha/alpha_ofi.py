import numpy as np
from numba import float64, njit
from numba.experimental import jitclass


@jitclass(
    [
        ("prev_bid_p", float64),
        ("prev_ask_p", float64),
        ("prev_bid_v", float64),
        ("prev_ask_v", float64),
        ("ofi", float64),
        ("obi", float64),
    ]
)
class AlphaOFI:
    def __init__(self):
        self.prev_bid_p = np.nan
        self.prev_ask_p = np.nan
        self.prev_bid_v = 0.0
        self.prev_ask_v = 0.0
        self.ofi = 0.0
        self.obi = 0.0

    def update(self, bid_p, ask_p, bid_v, ask_v):
        # Initialize if first tick
        if np.isnan(self.prev_bid_p):
            self.prev_bid_p = bid_p
            self.prev_ask_p = ask_p
            self.prev_bid_v = bid_v
            self.prev_ask_v = ask_v
            return

        # OFI Calculation (Cont et al. 2014)
        # Bid leg
        e_bid = 0.0
        if bid_p > self.prev_bid_p:
            e_bid = bid_v
        elif bid_p < self.prev_bid_p:
            e_bid = -self.prev_bid_v
        else:
            e_bid = bid_v - self.prev_bid_v

        # Ask leg
        e_ask = 0.0
        if ask_p < self.prev_ask_p:
            e_ask = ask_v
        elif ask_p > self.prev_ask_p:
            e_ask = -self.prev_ask_v
        else:
            e_ask = ask_v - self.prev_ask_v

        # OFI = e_bid - e_ask
        self.ofi = e_bid - e_ask

        # OBI Calculation (Order Book Imbalance)
        # (BidVol - AskVol) / (BidVol + AskVol)
        total_vol = bid_v + ask_v
        if total_vol > 0:
            self.obi = (bid_v - ask_v) / total_vol
        else:
            self.obi = 0.0

        # Update state persistence
        self.prev_bid_p = bid_p
        self.prev_ask_p = ask_p
        self.prev_bid_v = bid_v
        self.prev_ask_v = ask_v


@njit
def ofi_strategy(hbt):
    # Parameter setup
    asset_no = 0
    alpha = AlphaOFI()

    # In hftbacktest, we usually iterate until the end
    # Elapse wait time (e.g. 1 tick or a small interval)
    # Using a 1ms loop for demonstration, but OFI is tick-based.
    # To be truly tick-based, we should ideally check for events.
    # 'elapse' without doing anything just advances time.
    # However, hftbacktest doesn't strictly have a 'on_tick' callback in the while-loop style
    # unless we use `wait_next_feed` or similar, but the standard pattern is checking periodically or on response.
    # For Alpha calculation that should be continuous, we can use a tight loop or check `hbt.wait_next_feed()`.

    # Using 100 microseconds step for high fidelity
    while hbt.elapse(100_000) == 0:
        # 1. Housekeeping
        hbt.clear_inactive_orders(asset_no)

        # 2. Market Data Access
        depth = hbt.depth(asset_no)

        # Guard against empty depth (start of day)
        if depth.best_bid == 0.0 or depth.best_ask == 0.0:
            continue

        # 3. Update Alpha State
        alpha.update(depth.best_bid, depth.best_ask, depth.best_bid_qty, depth.best_ask_qty)

        # 4. Record Stats (Optional, for debugging)
        # We can write to 'stat' array if provided
        # stat.record(hbt.current_timestamp, alpha.ofi, alpha.obi)

        # Example Trading Logic (Simple Momentum)
        # If OFI > threshold, Buy
        # If OFI < -threshold, Sell
        # This is strictly for validaiton

    return True


strategy = ofi_strategy
