import numpy as np
from numba import float64, int64, njit
from numba.experimental import jitclass

K = 3


@jitclass(
    [
        ("weights", float64[:]),
        ("betas", float64[:]),
        ("components", float64[:]),
        ("last_ts", int64),
        ("total_impact", float64),
    ]
)
class PropagatorTracker:
    def __init__(self):
        self.weights = np.array([0.5, 0.3, 0.2], dtype=np.float64)
        self.betas = np.array([100.0, 10.0, 1.0], dtype=np.float64)
        self.components = np.zeros(K, dtype=np.float64)
        self.last_ts = 0
        self.total_impact = 0.0

    def update(self, current_ts):
        """Decay estimates up to current_ts"""
        if self.last_ts == 0:
            self.last_ts = current_ts
            return

        dt_ns = current_ts - self.last_ts
        if dt_ns > 0:
            dt_sec = float(dt_ns) * 1e-9

            for k in range(K):
                decay = np.exp(-self.betas[k] * dt_sec)
                self.components[k] *= decay

            self.last_ts = current_ts
            self.recalc_total()

    def add_event(self, sign, qty):
        """Add trade impact"""
        impact_magnitude = sign * np.log(1.0 + qty)

        for k in range(K):
            self.components[k] += self.weights[k] * impact_magnitude

        self.recalc_total()

    def recalc_total(self):
        sum_val = 0.0
        for k in range(K):
            sum_val += self.components[k]
        self.total_impact = sum_val


@njit
def strategy(hbt):
    asset_no = 0
    tracker = PropagatorTracker()

    while hbt.elapse(1_000_000) == 0:
        current_ts = hbt.current_timestamp

        tracker.update(current_ts)

        trades = hbt.last_trades(asset_no)

        for i in range(len(trades)):
            trade = trades[i]

            sign = float(trade.ival)
            qty = float(trade.qty)

            tracker.add_event(sign, qty)

        hbt.clear_last_trades(asset_no)

    return True
