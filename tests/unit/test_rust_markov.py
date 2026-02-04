
import pytest
import numpy as np
import sys
import os
from numba import njit

# Ensure we can import from src
sys.path.append(os.getcwd())

try:
    from hft_platform.rust_core import AlphaMarkovTransition
except ImportError:
    pytest.fail("Could not import hft_platform.rust_core. Ensure maturin develop was run.")

# --- Embedded Reference Logic (from research/tools/factor_registry.py) ---
@njit(cache=False, nogil=True)
def _reference_markov_numba(returns: np.ndarray) -> np.ndarray:
    n = len(returns)
    states = np.zeros(n, dtype=np.int8)
    for i in range(n):
        if returns[i] > 0:
            states[i] = 1
        elif returns[i] < 0:
            states[i] = -1
        else:
            states[i] = 0
            
    signal = np.zeros(n)
    
    # Adaptive Expectations
    est_up = 0.0
    est_dn = 0.0
    est_flat = 0.0
    alpha = 0.02
    
    # We predict r[i+1] based on s[i]
    # signal[i] is the belief at time i about r[i+1]
    
    for i in range(n - 1):
        s = states[i]
        target = returns[i+1] # Next return
        
        if s == 1:
            signal[i] = est_up
            est_up = est_up * (1 - alpha) + target * alpha
        elif s == -1:
            signal[i] = est_dn
            est_dn = est_dn * (1 - alpha) + target * alpha
        else:
            signal[i] = est_flat
            est_flat = est_flat * (1 - alpha) + target * alpha
            
    return signal

def test_markov_parity_rust_vs_numba():
    """
    Verify that the Rust implementation of MarkovTransition 
    matches the logic of the Python implementation.
    """
    np.random.seed(42)
    n = 10000
    
    # Generate prices 
    # Random walk
    mid_price = 100.0 + np.cumsum(np.random.randn(n) * 0.1)
    
    # Construct data dict
    bid_p = (mid_price - 0.01).reshape(-1, 1)
    ask_p = (mid_price + 0.01).reshape(-1, 1)
        
    # Re-derive returns to pass to Rust as it expects "returns" input
    # Same logic as Reference
    mid = (bid_p[:, 0] + ask_p[:, 0]) / 2.0
    returns = np.diff(mid, prepend=mid[0])
    
    # 1. Compute Reference Version
    py_result = _reference_markov_numba(returns)
    
    # 2. Compute Rust Version
    # Pass alpha=0.02 which is hardcoded in Reference
    rust_factor = AlphaMarkovTransition(alpha=0.02)
    
    rust_result = rust_factor.compute(returns)
    
    # 3. Compare with strict tolerance
    # Assumption from logic audit: signal should match exactly.
    
    np.testing.assert_allclose(py_result, rust_result, rtol=1e-10, atol=1e-10, 
                               err_msg="Rust MarkovTransition deviation")
    
    print("\nParity Confirmed: Python == Rust")

if __name__ == "__main__":
    test_markov_parity_rust_vs_numba()
