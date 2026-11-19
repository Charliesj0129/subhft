import numpy as np
from numba import float64, int64, njit
from numba.experimental import jitclass

NUM_ASSETS = 2


@jitclass(
    [
        ("mu", float64[:]),
        ("alpha", float64[:, :]),
        ("beta", float64[:]),
        ("intensities", float64[:]),
        ("last_ts", int64[:]),
    ]
)
class MHPTracker:
    def __init__(self, mu, alpha, beta):
        self.mu = mu
        self.alpha = alpha
        self.beta = beta
        self.intensities = np.copy(mu)
        self.last_ts = np.zeros(NUM_ASSETS, dtype=np.int64)

    def update_decay(self, asset_idx, current_ts):
        """Apply decay to specific intensity dimension up to current_ts"""
        prev_ts = self.last_ts[asset_idx]
        if prev_ts == 0:
            self.last_ts[asset_idx] = current_ts
            return

        dt_ns = current_ts - prev_ts
        if dt_ns > 0:
            dt_sec = float(dt_ns) * 1e-9
            decay_factor = np.exp(-self.beta[asset_idx] * dt_sec)

            curr_lambda = self.intensities[asset_idx]
            base_mu = self.mu[asset_idx]

            new_lambda = base_mu + (curr_lambda - base_mu) * decay_factor
            self.intensities[asset_idx] = new_lambda
            self.last_ts[asset_idx] = current_ts

    def trigger_excitation(self, source_idx):
        """Event on source_idx triggers jumps on ALL dimensions"""
        for i in range(NUM_ASSETS):
            self.intensities[i] += self.alpha[i, source_idx]


@njit
def mhp_strategy(hbt):
    mu = np.array([1.0, 1.0], dtype=np.float64)

    alpha = np.array(
        [
            [0.5, 0.1],
            [0.8, 0.5],
        ],
        dtype=np.float64,
    )

    beta = np.array([10.0, 10.0], dtype=np.float64)

    tracker = MHPTracker(mu, alpha, beta)

    while hbt.elapse(1_000_000) == 0:
        current_ts = hbt.current_timestamp

        for i in range(NUM_ASSETS):
            tracker.update_decay(i, current_ts)

        for asset_id in range(NUM_ASSETS):
            trades = hbt.last_trades(asset_id)
            if len(trades) > 0:
                tracker.trigger_excitation(asset_id)
                hbt.clear_last_trades(asset_id)

    return True


strategy = mhp_strategy
