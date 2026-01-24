import numpy as np
from numba import float64, int64, njit
from numba.experimental import jitclass

# K=3 for Sum of Exponentials approximation
K = 3


@jitclass(
    [
        ("weights", float64[:]),  # Weights w_k
        ("betas", float64[:]),  # Decay rates beta_k
        ("components", float64[:]),  # Current value of each component S_k
        ("last_ts", int64),  # Last update timestamp
        ("total_impact", float64),  # Current sum I(t)
    ]
)
class PropagatorTracker:
    def __init__(self):
        # Configuration for timescales:
        # Fast: ~10ms (beta=100)
        # Medium: ~100ms (beta=10)
        # Slow: ~1s (beta=1)
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

            # Recursive decay for each component
            # S_k(t) = S_k(prev) * exp(-beta_k * dt)
            for k in range(K):
                decay = np.exp(-self.betas[k] * dt_sec)
                self.components[k] *= decay

            self.last_ts = current_ts
            self.recalc_total()

    def add_event(self, sign, qty):
        """Add trade impact"""
        # Impact usually scales with sign * f(qty).
        # Linear or sqrt(qty). Let's use simple sign * log(1+qty) for concave impact.
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

    # Run at 1ms resolution
    while hbt.elapse(1_000_000) == 0:
        current_ts = hbt.current_timestamp

        # 1. Decay state
        tracker.update(current_ts)

        # 2. Process recent trades
        # Note: In a real strategy, we might process trades individually
        # using wait_order_response or checking flags, but hbt.last_trades is efficient for batches.
        trades = hbt.last_trades(asset_no)

        # Iterate manually through the struct array to avoid Numba array creation overhead if possible,
        # but iterating the array provided by hbt is fine.
        for i in range(len(trades)):
            trade = trades[i]
            # Trade direction logic
            # hftbacktest uses flags. usually if trade price >= ask => buy?
            # Or use 'ival' field if available/reliable (1=buy, -1=sell)
            # Standard hbt format usually has ival in the event_dtype?
            # Looking at previous view_code_item event_dtype: yes, it likely has flags.
            # But specific to trade direction:
            # If (trade_px >= depth.best_ask) -> Buy Aggressor (Impact moves price UP)
            # If (trade_px <= depth.best_bid) -> Sell Aggressor (Impact moves price DOWN)
            # Simpler: use the 'ival' provided by data loader if it set it correct.
            # In synthetic gen, we set ival=1 for buy, -1 for sell.

            # Assuming ival is correctly populated (bit 2 or just signed int depending on version)
            # For this synthetic data, we used ival 1 or -1 explicitly.

            sign = float(trade.ival)
            qty = float(trade.qty)

            tracker.add_event(sign, qty)

        hbt.clear_last_trades(asset_no)

        # 3. Strategy Logic (Example)
        # If predicted structural impact is high positive => Price likely supported/moving up?
        # Or mean reverting?
        # Propagator usually measures "Price Pressure".
        # If Impact > Threshold -> Long?

    return True
