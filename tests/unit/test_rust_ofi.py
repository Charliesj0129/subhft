import os
import sys

import numpy as np
import pytest

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

    mid_price = 100.0 + np.cumsum(np.random.randn(n) * 0.1)
    spread = 0.02

    bid_p = mid_price - spread / 2
    ask_p = mid_price + spread / 2

    bid_p = np.round(bid_p, 2)
    ask_p = np.round(ask_p, 2)

    bid_v = np.abs(np.random.randn(n) * 10) + 1
    ask_v = np.abs(np.random.randn(n) * 10) + 1

    data = {
        "bid_prices": bid_p.reshape(-1, 1),
        "ask_prices": ask_p.reshape(-1, 1),
        "bid_volumes": bid_v.reshape(-1, 1),
        "ask_volumes": ask_v.reshape(-1, 1),
    }

    py_factor = OFIFactor()

    py_result = py_factor.compute(data)

    rust_factor = AlphaOFI()

    rust_result = rust_factor.compute(bid_p, ask_p, bid_v, ask_v)

    np.testing.assert_allclose(
        py_result,
        rust_result,
        rtol=1e-10,
        atol=1e-10,
        err_msg="Rust OFI does not match Python OFI",
    )

    print("\nParity Confirmed: Python OFI == Rust OFI (bid_flow - ask_flow)")


if __name__ == "__main__":
    test_ofi_parity_rust_vs_numba()
