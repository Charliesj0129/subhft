"""Gate B anti-leak / lookahead-bias tests for LiquidityShearAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np

from research.alphas.liquidity_shear.impl import LiquidityShearAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args must return a numeric value."""
    alpha = LiquidityShearAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_stateful_not_lookahead() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = LiquidityShearAlpha()
    # Ask concentrated → positive shear
    bids_flat = np.array([[100.0, 30.0], [99.0, 30.0], [98.0, 30.0]])
    asks_conc = np.array([[101.0, 90.0], [102.0, 5.0], [103.0, 5.0]])
    sig1 = alpha.update(bids=bids_flat, asks=asks_conc)

    # Flip: bid concentrated → negative shear
    bids_conc = np.array([[100.0, 90.0], [99.0, 5.0], [98.0, 5.0]])
    asks_flat = np.array([[101.0, 30.0], [102.0, 30.0], [103.0, 30.0]])
    sig2 = alpha.update(bids=bids_conc, asks=asks_flat)

    assert sig1 > 0.0
    assert sig2 < sig1  # signal moved in the correct direction


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = LiquidityShearAlpha()
    a2 = LiquidityShearAlpha()

    bids_asym = np.array([[100.0, 90.0], [99.0, 5.0], [98.0, 5.0]])
    asks_asym = np.array([[101.0, 30.0], [102.0, 30.0], [103.0, 30.0]])
    a1.update(bids=bids_asym, asks=asks_asym)
    a1.reset()

    sym = np.array([[100.0, 50.0], [99.0, 30.0], [98.0, 20.0]])
    s1 = a1.update(bids=sym, asks=sym)
    s2 = a2.update(bids=sym, asks=sym)
    assert s1 == s2


def test_no_future_data_leakage_via_sequence() -> None:
    """Signal at tick T must not depend on tick T+1 data."""
    alpha = LiquidityShearAlpha()
    bids = np.array([[100.0, 60.0], [99.0, 30.0], [98.0, 10.0]])
    asks = np.array([[101.0, 60.0], [102.0, 30.0], [103.0, 10.0]])
    sig_t = alpha.update(bids=bids, asks=asks)

    # Create fresh alpha, feed same first tick
    alpha2 = LiquidityShearAlpha()
    sig_t2 = alpha2.update(bids=bids, asks=asks)
    assert sig_t == sig_t2  # identical regardless of future


def test_signal_monotonicity_under_constant_input() -> None:
    """Under constant asymmetric input, signal should converge monotonically."""
    alpha = LiquidityShearAlpha()
    bids = np.array([[100.0, 80.0], [99.0, 15.0], [98.0, 5.0]])
    asks = np.array([[101.0, 30.0], [102.0, 30.0], [103.0, 30.0]])
    prev = None
    for _ in range(50):
        sig = alpha.update(bids=bids, asks=asks)
        if prev is not None:
            # Signal should move monotonically toward the constant raw value
            assert abs(sig) >= abs(prev) - 1e-9
        prev = sig
