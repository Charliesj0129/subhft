import numpy as np
from numba import float64, int64, njit
from numba.experimental import jitclass


@jitclass([("mu", float64), ("alpha", float64), ("beta", float64), ("last_ts", int64), ("intensity", float64)])
class HawkesTracker:
    def __init__(self, mu, alpha, beta):
        self.mu = mu
        self.alpha = alpha
        self.beta = beta
        self.last_ts = 0
        self.intensity = mu

    def update(self, current_ts, is_event):
        if self.last_ts == 0:
            self.last_ts = current_ts
            return

        dt_ns = current_ts - self.last_ts

        if dt_ns < 0:
            dt_ns = 0

        exponent = -self.beta * float(dt_ns) * 1e-9
        decay = np.exp(exponent)

        self.intensity = self.mu + (self.intensity - self.mu) * decay

        if is_event:
            self.intensity += self.alpha

        self.last_ts = current_ts


@njit
def hawkes_strategy(hbt):
    asset_no = 0

    tracker = HawkesTracker(1.0, 0.5, 10.0)

    while hbt.elapse(1_000_000) == 0:
        current_ts = hbt.current_timestamp

        trades = hbt.last_trades(asset_no)
        is_trade_event = False

        if len(trades) > 0:
            is_trade_event = True

        tracker.update(current_ts, is_trade_event)

        hbt.clear_last_trades(asset_no)

        if tracker.intensity > 5.0:
            pass

    return True


strategy = hawkes_strategy
