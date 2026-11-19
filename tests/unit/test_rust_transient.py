import os
import sys

import numpy as np
import pytest

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

    mid_price = 100.0 + np.cumsum(np.random.randn(n) * 0.1)
    spread = 0.02

    bid_p = mid_price - spread / 2
    ask_p = mid_price + spread / 2

    bid_v = np.ones(n)
    ask_v = np.ones(n)

    data = {
        "bid_prices": bid_p.reshape(-1, 1),
        "ask_prices": ask_p.reshape(-1, 1),
        "bid_volumes": bid_v.reshape(-1, 1),
        "ask_volumes": ask_v.reshape(-1, 1),
    }

    py_factor = TransientRepriceFactor()
    py_result = py_factor.compute(data)

    rust_factor = AlphaTransientReprice(window_size=10)

    rust_result = rust_factor.compute(bid_p, ask_p)

    k = 10

    np.testing.assert_allclose(
        py_result[k:], rust_result[k:], rtol=1e-10, atol=1e-10, err_msg="Rust TransientReprice deviation"
    )

    print("\nParity Confirmed: Python == Rust")


if __name__ == "__main__":
    test_transient_parity_rust_vs_numba()
