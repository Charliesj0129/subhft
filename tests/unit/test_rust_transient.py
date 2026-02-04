
import os
import sys

import numpy as np
import pytest

# Ensure we can import from src
sys.path.append(os.getcwd())


factor_registry = pytest.importorskip("research.tools.factor_registry", reason="research tools not available")
TransientRepriceFactor = factor_registry.TransientRepriceFactor
try:
    from hft_platform.rust_core import AlphaTransientReprice
except ImportError:
    pytest.fail("Could not import hft_platform.rust_core. Ensure maturin develop was run.")

def test_transient_parity_rust_vs_numba():
    """
    Verify that the Rust implementation of TransientReprice
    matches the logic of the Python implementation.
    """
    np.random.seed(42)
    n = 10000

    # Generate prices with some trend and reversion
    # Random walk
    mid_price = 100.0 + np.cumsum(np.random.randn(n) * 0.1)
    spread = 0.02

    bid_p = mid_price - spread/2
    ask_p = mid_price + spread/2

    # Volumes (not used in this factor but passed potentially)
    bid_v = np.ones(n)
    ask_v = np.ones(n)

    # Construct data dict
    data = {
        "bid_prices": bid_p.reshape(-1, 1),
        "ask_prices": ask_p.reshape(-1, 1),
        "bid_volumes": bid_v.reshape(-1, 1),
        "ask_volumes": ask_v.reshape(-1, 1),
    }

    # 1. Compute Python Version
    py_factor = TransientRepriceFactor()
    # Note: Python hardcodes k=10
    py_result = py_factor.compute(data)

    # 2. Compute Rust Version
    # Pass window_size = 10 explicitly
    rust_factor = AlphaTransientReprice(window_size=10)

    rust_result = rust_factor.compute(bid_p, ask_p)

    # 3. Compare with strict tolerance
    # We expect exact match or near-exact due to f64 precision

    # Ignore the first k=10 elements where transient logic might leave zeros or different init
    k = 10

    # Note: The Python implementation does:
    # ret[k:] = ...
    # ret[:k] are zeros
    # Rust output logic:
    # for t in k..n: ...
    # signal initialized to zeros.
    # So both should have zeros in first k elements.

    np.testing.assert_allclose(py_result[k:], rust_result[k:], rtol=1e-10, atol=1e-10,
                               err_msg="Rust TransientReprice deviation")

    print("\nParity Confirmed: Python == Rust")

if __name__ == "__main__":
    test_transient_parity_rust_vs_numba()
