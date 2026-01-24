import numpy as np
from numba import njit, float64, int64, void
from numba.experimental import jitclass
from hftbacktest import HashMapMarketDepthBacktest

# Constants
NUM_ASSETS = 2

@jitclass([
    ('mu', float64[:]),      # Base intensity (M)
    ('alpha', float64[:,:]), # Excitation matrix (MxM)
    ('beta', float64[:]),    # Decay rates (M) - Diagonal assumption
    ('intensities', float64[:]), # Current intensities (M)
    ('last_ts', int64[:])    # Last update time per dimension (M)
])
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
            # Decay: lambda(t) = mu + (lambda(t_prev) - mu) * exp(-beta * dt)
            dt_sec = float(dt_ns) * 1e-9
            decay_factor = np.exp(-self.beta[asset_idx] * dt_sec)
            
            curr_lambda = self.intensities[asset_idx]
            base_mu = self.mu[asset_idx]
            
            new_lambda = base_mu + (curr_lambda - base_mu) * decay_factor
            self.intensities[asset_idx] = new_lambda
            self.last_ts[asset_idx] = current_ts

    def trigger_excitation(self, source_idx):
        """Event on source_idx triggers jumps on ALL dimensions"""
        # For each target dimension i, add alpha[i, source_idx]
        for i in range(NUM_ASSETS):
            # Jump happens instantaneously after decay
            self.intensities[i] += self.alpha[i, source_idx]

@njit
def mhp_strategy(hbt):
    # Setup MHP parameters (2 assets)
    # 0: TXF, 1: MXF
    mu = np.array([1.0, 1.0], dtype=np.float64)
    
    # Alpha Matrix: M x M
    # Row i, Col j: Influence of j on i
    # [[Self-TXF, MXF->TXF],
    #  [TXF->MXF, Self-MXF]]
    alpha = np.array([
        [0.5, 0.1],  # TXF self-excited=0.5, MXF triggers TXF=0.1
        [0.8, 0.5]   # TXF triggers MXF strongly (0.8)
    ], dtype=np.float64)
    
    beta = np.array([10.0, 10.0], dtype=np.float64) # Fast decay
    
    tracker = MHPTracker(mu, alpha, beta)
    
    # Run loop
    # We must check each asset for new trades
    
    # To correctly handle time, we should process event-by-event.
    # But 'elapse' jumps time.
    # Better approach for MHP: use 'wait_next_feed' to get EXACT event time?
    # But for simplicity & hftbacktest standard structure, we check buffer.
    
    while hbt.elapse(1_000_000) == 0: # 1ms
        current_ts = hbt.current_timestamp
        
        # Check all assets
        for i in range(NUM_ASSETS):
            # First, bring intensity up to date (decay)
            tracker.update_decay(i, current_ts)
        
        # Check for events
        for asset_id in range(NUM_ASSETS):
            trades = hbt.last_trades(asset_id)
            if len(trades) > 0:
                # Excitation!
                # Since hbt.last_trades merges batch, we treat it as 1 discrete event or N?
                # We treat as 1 excitation for the batch for perf.
                tracker.trigger_excitation(asset_id)
                hbt.clear_last_trades(asset_id)
                
        # Logic: If MXF intensity is high but TXF is low -> arbitrage?
        
    return True

strategy = mhp_strategy
