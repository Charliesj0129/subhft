import numpy as np
from numba import njit, float64, int64
from numba.experimental import jitclass
from hftbacktest import HashMapMarketDepthBacktest

# Architecture Params
INPUT_DIM = 3   # [sign, log_qty, dt]
HIDDEN_DIM = 32 # Higher capacity as requested

@jitclass([
    # LSTM Weights (Concatenated for [Wf, Wi, Wo, Wc])
    # Total rows = 4 * HIDDEN_DIM
    # Total cols = INPUT_DIM + HIDDEN_DIM
    ('W', float64[:,:]), 
    ('b', float64[:]),
    
    # Output Layer
    ('W_out', float64[:]), # HIDDEN -> 1
    ('b_out', float64),
    
    # State
    ('h', float64[:]),
    ('c', float64[:]),
    ('last_ts', int64),
    ('intensity', float64)
])
class LSTMTracker:
    def __init__(self):
        # Initialize random weights (Simulating a pre-trained model loading)
        # 4 gates * 32 units = 128 rows
        # 3 input + 32 hidden = 35 cols
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
        # In Neural Hawkes, time is usually an input feature "dt" delta between events.
        # So we don't decay continuously like Hawkes, we step on events.
        # But we DO need to track last_ts to compute dt for the next event.
        if self.last_ts == 0:
            self.last_ts = current_ts

    def step(self, sign, qty, current_ts):
        """LSTM Step on Event"""
        dt_ns = current_ts - self.last_ts
        dt_sec = float(dt_ns) * 1e-9
        
        # 1. Prepare Input Vector x_concat = [x_t, h_{t-1}]
        # x_t = [sign, log(1+qty), dt]
        log_qty = np.log(1.0 + qty)
        
        # We need to perform W @ concat(x, h) + b
        # Let's do it manually or efficiently.
        # x vector size 3. h vector size 32.
        
        # Pre-activation vector for all 4 gates: (4*H)
        # gates_linear = W[:, :3] @ x + W[:, 3:] @ h + b
        
        # Optimizing: Splitting computation
        # part1 = W_x @ x
        # part2 = W_h @ h
        
        # W has shape (128, 35).
        # Split into Wx (128, 3) and Wh (128, 32) implicitly
        
        # Compute gates_linear (size 128)
        gates_raw = np.zeros(4 * HIDDEN_DIM, dtype=np.float64)
        
        # Wx part
        # Unroll for I=3 inputs
        inputs = np.array([sign, log_qty, dt_sec], dtype=np.float64)
        for r in range(4 * HIDDEN_DIM):
            val = self.b[r]
            val += self.W[r, 0] * inputs[0]
            val += self.W[r, 1] * inputs[1]
            val += self.W[r, 2] * inputs[2]
            
            # Wh part (dot product with h)
            # Row r, columns 3..34 correspond to h
            for c in range(HIDDEN_DIM):
                val += self.W[r, 3 + c] * self.h[c]
            
            gates_raw[r] = val
            
        # 2. Apply Activations (Slice gates)
        # f_t = sigmoid(gates[0:H])
        # i_t = sigmoid(gates[H:2H])
        # o_t = sigmoid(gates[2H:3H])
        # g_t = tanh(gates[3H:4H]) (cell candidate)
        
        for i in range(HIDDEN_DIM):
            idx_f = i
            idx_i = HIDDEN_DIM + i
            idx_o = 2 * HIDDEN_DIM + i
            idx_g = 3 * HIDDEN_DIM + i
            
            f_t = self.sigmoid(gates_raw[idx_f])
            i_t = self.sigmoid(gates_raw[idx_i])
            o_t = self.sigmoid(gates_raw[idx_o])
            g_t = self.tanh_activation(gates_raw[idx_g])
            
            # 3. Update Cell State
            # c_t = f_t * c_{t-1} + i_t * g_t
            self.c[i] = f_t * self.c[i] + i_t * g_t
            
            # 4. Update Hidden State
            # h_t = o_t * tanh(c_t)
            self.h[i] = o_t * self.tanh_activation(self.c[i])
            
        # 5. Compute Output Intensity
        # lambda = softplus(W_out @ h + b_out)
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
            
            # Feed event to LSTM
            tracker.step(sign, qty, current_ts)
            
        hbt.clear_last_trades(asset_no)
        
        # Strategy logic: If intensity high, do something
        if tracker.intensity > 10.0:
            pass 
            
    return True
