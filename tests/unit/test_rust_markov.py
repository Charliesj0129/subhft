import os
import sys

import numpy as np
import pytest
from numba import njit

sys.path.append(os.getcwd())

try:
    from hft_platform.rust_core import AlphaMarkovTransition
except ImportError:
    pytest.fail("Could not import hft_platform.rust_core. Ensure maturin develop was run.")


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

    est_up = 0.0
    est_dn = 0.0
    est_flat = 0.0
    alpha = 0.02

    for i in range(n - 1):
        s = states[i]
        target = returns[i + 1]

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

    mid_price = 100.0 + np.cumsum(np.random.randn(n) * 0.1)

    bid_p = (mid_price - 0.01).reshape(-1, 1)
    ask_p = (mid_price + 0.01).reshape(-1, 1)

    mid = (bid_p[:, 0] + ask_p[:, 0]) / 2.0
    returns = np.diff(mid, prepend=mid[0])

    py_result = _reference_markov_numba(returns)

    rust_factor = AlphaMarkovTransition(alpha=0.02)

    rust_result = rust_factor.compute(returns)

    np.testing.assert_allclose(
        py_result, rust_result, rtol=1e-10, atol=1e-10, err_msg="Rust MarkovTransition deviation"
    )

    print("\nParity Confirmed: Python == Rust")


if __name__ == "__main__":
    test_markov_parity_rust_vs_numba()
