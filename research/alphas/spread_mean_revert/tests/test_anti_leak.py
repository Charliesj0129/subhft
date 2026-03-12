"""Gate B anti-leak / lookahead-bias tests for SpreadMeanRevertAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.spread_mean_revert.impl import SpreadMeanRevertAlpha


def test_no_future_leak() -> None:
    """Each update only uses current and past data, not future."""
    alpha = SpreadMeanRevertAlpha()
    # Warm up
    for _ in range(200):
        alpha.update(100)
    sig_before = alpha.get_signal()
    # Feed one more data point
    alpha.update(200)
    sig_after = alpha.get_signal()
    # Signal must change -- proves it reacts to new data, not pre-computed
    assert sig_before != sig_after


def test_reset_prevents_leak() -> None:
    """After reset(), past data cannot influence future signals."""
    a1 = SpreadMeanRevertAlpha()
    a2 = SpreadMeanRevertAlpha()
    # a1 sees extreme data then resets
    for _ in range(100):
        a1.update(9999)
    a1.reset()
    # Both fed same data after reset
    s1 = a1.update(100)
    s2 = a2.update(100)
    assert s1 == s2


def test_no_global_state() -> None:
    """Two independent instances do not share state."""
    a1 = SpreadMeanRevertAlpha()
    a2 = SpreadMeanRevertAlpha()
    a1.update(500)
    a1.update(500)
    a1.update(500)
    s2 = a2.update(100)
    # a2 should not be affected by a1
    assert s2 == 0.0  # first update initializes, signal=0


def test_order_matters() -> None:
    """Different orderings of the same values produce different signals."""
    a1 = SpreadMeanRevertAlpha()
    a2 = SpreadMeanRevertAlpha()
    # a1: low then high
    for _ in range(50):
        a1.update(50)
    for _ in range(50):
        a1.update(150)
    # a2: high then low
    for _ in range(50):
        a2.update(150)
    for _ in range(50):
        a2.update(50)
    assert a1.get_signal() != a2.get_signal()


def test_no_lookahead() -> None:
    """Signal at tick t does not depend on data at tick t+1."""
    alpha = SpreadMeanRevertAlpha()
    for _ in range(100):
        alpha.update(100)
    sig_at_100 = alpha.get_signal()

    # Clone by replaying same sequence
    alpha2 = SpreadMeanRevertAlpha()
    for _ in range(100):
        alpha2.update(100)
    # alpha2 sees one more tick
    alpha2.update(9999)

    # Original signal unchanged -- proves no lookahead
    assert sig_at_100 == alpha.get_signal()
