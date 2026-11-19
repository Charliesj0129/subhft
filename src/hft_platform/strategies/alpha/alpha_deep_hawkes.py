import numpy as np
from numba import float64, int64, njit
from numba.experimental import jitclass

INPUT_DIM = 3
HIDDEN_DIM = 32


@jitclass(
    [
        ("W", float64[:, :]),
        ("b", float64[:]),
        ("W_out", float64[:]),
        ("b_out", float64),
        ("h", float64[:]),
        ("c", float64[:]),
        ("last_ts", int64),
        ("intensity", float64),
    ]
)
class LSTMTracker:
    def __init__(self):
        self.W = np.random.uniform(-0.1, 0.1, (4 * HIDDEN_DIM, INPUT_DIM + HIDDEN_DIM))
        self.b = np.zeros(4 * HIDDEN_DIM, dtype=np.float64)

        self.W_out = np.random.uniform(-0.1, 0.1, HIDDEN_DIM)
        self.b_out = 0.0

        self.h = np.zeros(HIDDEN_DIM, dtype=np.float64)
        self.c = np.zeros(HIDDEN_DIM, dtype=np.float64)
        self.last_ts = 0
        self.intensity = 0.0

    def sigmoid(self, x):
        return 1.0 / (1.0 + np.exp(-x))

    def tanh_activation(self, x):
        return np.tanh(x)

    def softplus(self, x):
        return np.log(1.0 + np.exp(x))

    def update(self, current_ts):
        """Time Decay or just State Update?"""
        if self.last_ts == 0:
            self.last_ts = current_ts

    def step(self, sign, qty, current_ts):
        """LSTM Step on Event"""
        dt_ns = current_ts - self.last_ts
        dt_sec = float(dt_ns) * 1e-9

        log_qty = np.log(1.0 + qty)

        gates_raw = np.zeros(4 * HIDDEN_DIM, dtype=np.float64)

        inputs = np.array([sign, log_qty, dt_sec], dtype=np.float64)
        for r in range(4 * HIDDEN_DIM):
            val = self.b[r]
            val += self.W[r, 0] * inputs[0]
            val += self.W[r, 1] * inputs[1]
            val += self.W[r, 2] * inputs[2]

            for c in range(HIDDEN_DIM):
                val += self.W[r, 3 + c] * self.h[c]

            gates_raw[r] = val

        for i in range(HIDDEN_DIM):
            idx_f = i
            idx_i = HIDDEN_DIM + i
            idx_o = 2 * HIDDEN_DIM + i
            idx_g = 3 * HIDDEN_DIM + i

            f_t = self.sigmoid(gates_raw[idx_f])
            i_t = self.sigmoid(gates_raw[idx_i])
            o_t = self.sigmoid(gates_raw[idx_o])
            g_t = self.tanh_activation(gates_raw[idx_g])

            self.c[i] = f_t * self.c[i] + i_t * g_t

            self.h[i] = o_t * self.tanh_activation(self.c[i])

        out_linear = self.b_out
        for i in range(HIDDEN_DIM):
            out_linear += self.W_out[i] * self.h[i]

        self.intensity = self.softplus(out_linear)
        self.last_ts = current_ts


@njit
def strategy(hbt):
    asset_no = 0
    tracker = LSTMTracker()

    while hbt.elapse(1_000_000) == 0:
        current_ts = hbt.current_timestamp
        tracker.update(current_ts)

        trades = hbt.last_trades(asset_no)
        for i in range(len(trades)):
            trade = trades[i]
            sign = float(trade.ival)
            qty = float(trade.qty)

            tracker.step(sign, qty, current_ts)

        hbt.clear_last_trades(asset_no)

        if tracker.intensity > 10.0:
            pass

    return True
