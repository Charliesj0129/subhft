
import numpy as np
import pytest

factor_registry = pytest.importorskip("research.tools.factor_registry", reason="research tools not available")
_compute_matched_filter_flow = factor_registry._compute_matched_filter_flow
from rust_core import MatchedFilterTradeFlow


def test_rust_parity():
    """Compare Numba implementation vs Rust implementation"""
    # Create matching data
    n = 1000
    trade_vol = np.ones(n, dtype=np.float64) * 100.0
    trade_side = np.ones(n, dtype=np.float64)

    # Add spikes and variation
    np.random.seed(42)
    trade_vol += np.random.random(n) * 20.0
    trade_side = np.where(np.random.random(n) > 0.5, 1.0, -1.0)

    # Introduce major spike
    trade_vol[500] = 5000.0

    fast_w = 10
    slow_w = 100

    # 1. Compute Python (Reference)
    py_signal = _compute_matched_filter_flow(trade_vol, trade_side, fast_w, slow_w)

    # 2. Compute Rust
    rust_factor = MatchedFilterTradeFlow(fast_w, slow_w)
    rust_signal = np.zeros(n, dtype=np.float64)

    for i in range(n):
        rust_signal[i] = rust_factor.update(trade_vol[i], trade_side[i])

    # 3. Compare
    # Note: Rust implementation handles warmup slightly differently?
    # Let's check from where both have enough history (slow_window)
    valid_idx = slow_w + 5

    mse = np.mean((py_signal[valid_idx:] - rust_signal[valid_idx:])**2)
    print(f"MSE: {mse}")

    # Assert close
    np.testing.assert_allclose(py_signal[valid_idx:], rust_signal[valid_idx:], rtol=1e-5, atol=1e-8)

if __name__ == "__main__":
    test_rust_parity()
