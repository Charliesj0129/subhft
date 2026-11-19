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
        if np.isnan(self.prev_bid_p):
            self.prev_bid_p = bid_p
            self.prev_ask_p = ask_p
            self.prev_bid_v = bid_v
            self.prev_ask_v = ask_v
            return

        e_bid = 0.0
        if bid_p > self.prev_bid_p:
            e_bid = bid_v
        elif bid_p < self.prev_bid_p:
            e_bid = -self.prev_bid_v
        else:
            e_bid = bid_v - self.prev_bid_v

        e_ask = 0.0
        if ask_p < self.prev_ask_p:
            e_ask = ask_v
        elif ask_p > self.prev_ask_p:
            e_ask = -self.prev_ask_v
        else:
            e_ask = ask_v - self.prev_ask_v

        self.ofi = e_bid - e_ask

        total_vol = bid_v + ask_v
        if total_vol > 0:
            self.obi = (bid_v - ask_v) / total_vol
        else:
            self.obi = 0.0

        self.prev_bid_p = bid_p
        self.prev_ask_p = ask_p
        self.prev_bid_v = bid_v
        self.prev_ask_v = ask_v


@njit
def ofi_strategy(hbt):
    asset_no = 0
    alpha = AlphaOFI()

    while hbt.elapse(100_000) == 0:
        hbt.clear_inactive_orders(asset_no)

        depth = hbt.depth(asset_no)

        if depth.best_bid == 0.0 or depth.best_ask == 0.0:
            continue

        alpha.update(depth.best_bid, depth.best_ask, depth.best_bid_qty, depth.best_ask_qty)

    return True


strategy = ofi_strategy
