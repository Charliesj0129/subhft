"""Gate B anti-leak / lookahead-bias tests for DepthMomentumAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.depth_momentum.impl import DepthMomentumAlpha


def test_no_future_data_access() -> None:
    """Signal at tick t only uses data up to tick t."""
    alpha = DepthMomentumAlpha()
    signals: list[float] = []
    data = [(100, 100), (200, 100), (150, 150), (100, 200)]
    for bd, ad in data:
        signals.append(alpha.update(bd, ad))
    # Re-run with only first 2 ticks — signals[0:2] must match
    alpha2 = DepthMomentumAlpha()
    for i, (bd, ad) in enumerate(data[:2]):
        sig = alpha2.update(bd, ad)
        assert sig == signals[i]


def test_reset_prevents_leakage() -> None:
    """After reset, old state doesn't affect new signal."""
    a1 = DepthMomentumAlpha()
    a2 = DepthMomentumAlpha()
    # Pollute a1 with data
    a1.update(800.0, 100.0)
    a1.update(900.0, 50.0)
    a1.reset()
    # Both should produce identical results on same input
    s1 = a1.update(300.0, 300.0)
    s2 = a2.update(300.0, 300.0)
    assert s1 == s2


def test_no_global_state() -> None:
    """Two independent instances don't share state."""
    a1 = DepthMomentumAlpha()
    a2 = DepthMomentumAlpha()
    a1.update(100.0, 100.0)
    a1.update(500.0, 100.0)
    # a2 is fresh — first update should return 0
    sig2 = a2.update(300.0, 300.0)
    assert sig2 == 0.0  # first update always 0
    assert a1.get_signal() != a2.get_signal()


def test_update_order_matters() -> None:
    """Different sequences produce different signals."""
    a1 = DepthMomentumAlpha()
    a2 = DepthMomentumAlpha()
    # Sequence 1: bid grows then shrinks
    a1.update(100, 100)
    a1.update(200, 100)
    sig1 = a1.update(100, 100)
    # Sequence 2: bid shrinks then grows
    a2.update(100, 100)
    a2.update(100, 200)
    sig2 = a2.update(100, 100)
    assert sig1 != sig2


def test_no_lookahead_in_ema() -> None:
    """EMA is causal: adding future data changes only future signals."""
    alpha = DepthMomentumAlpha()
    alpha.update(100, 100)
    sig_t1 = alpha.update(200, 100)
    # Record signal at t=1
    alpha_full = DepthMomentumAlpha()
    alpha_full.update(100, 100)
    sig_t1_full = alpha_full.update(200, 100)
    # Signal at t=1 must be the same regardless of future
    assert sig_t1 == sig_t1_full
    # Now feed different future data
    alpha.update(300, 100)
    alpha_full.update(100, 300)
    # t=2 signals differ (different future), but t=1 was identical
    assert alpha.get_signal() != alpha_full.get_signal()
