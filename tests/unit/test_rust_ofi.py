
import os
import sys

import numpy as np
import pytest

# Ensure we can import from src
sys.path.append(os.getcwd())


factor_registry = pytest.importorskip("research.tools.factor_registry", reason="research tools not available")
OFIFactor = factor_registry.OFIFactor
try:
    from hft_platform.rust_core import AlphaOFI
except ImportError:
    pytest.fail("Could not import hft_platform.rust_core. Ensure maturin develop was run.")

def test_ofi_parity_rust_vs_numba():
    """
    Verify that the Rust implementation of OFI produces identical results
    to the Numba-optimized production version.
    """
    np.random.seed(42)
    n = 10000

    # Generate synthetic data
    # Random walk prices
    mid_price = 100.0 + np.cumsum(np.random.randn(n) * 0.1)
    spread = 0.02

    bid_p = mid_price - spread/2
    ask_p = mid_price + spread/2

    # Quantize to ticks (0.01) to simulate real market
    bid_p = np.round(bid_p, 2)
    ask_p = np.round(ask_p, 2)

    # Volumes
    bid_v = np.abs(np.random.randn(n) * 10) + 1
    ask_v = np.abs(np.random.randn(n) * 10) + 1

    # Construct data dict expected by OFIFactor
    # OFIFactor expects dictionary with (N, levels) arrays, using index 0
    data = {
        "bid_prices": bid_p.reshape(-1, 1),
        "ask_prices": ask_p.reshape(-1, 1),
        "bid_volumes": bid_v.reshape(-1, 1),
        "ask_volumes": ask_v.reshape(-1, 1),
    }

    # 1. Compute Python/Numba Version
    py_factor = OFIFactor()
    # Note: OFIFactor.compute returns -ofi (inverted).
    # But wait, looking at my previous edits:
    # "Inverted the OFI signal by returning -ofi instead of ofi"
    # To compare raw calculation correctness, we need to know what Rust implements.
    # Rust AlphaOFI.compute implements raw OFI (bid_flow - ask_flow).
    # So we should expect Rust result = -1 * Python result (if Python returns inverted).

    py_result = py_factor.compute(data)

    # 2. Compute Rust Version
    rust_factor = AlphaOFI()

    # Rust expects 1D arrays
    rust_result = rust_factor.compute(bid_p, ask_p, bid_v, ask_v)

    # 3. Compare
    # Check if Python result is inverted
    # Implementation recall: return -_compute_ofi_numba(...)
    # Rust implementation: return ofi

    # So we expect py_result == -rust_result
    np.testing.assert_allclose(py_result, -rust_result, rtol=1e-10, atol=1e-10,
                               err_msg="Rust OFI does not match Python OFI (sign inverted check)")

    print("\nParity Confirmed: Python(Inverted) == -1 * Rust(Raw)")

if __name__ == "__main__":
    test_ofi_parity_rust_vs_numba()
