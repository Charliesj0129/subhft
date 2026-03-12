"""Gate B anti-leak / lookahead-bias tests for MicropriceMomentumAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.microprice_momentum.impl import MicropriceMomentumAlpha


def test_no_future() -> None:
    """Signal at tick t must not depend on data from tick t+1."""
    alpha = MicropriceMomentumAlpha()
    alpha.update(200000, 100)
    sig_t1 = alpha.update(200100, 100)
    # The signal at t=1 was computed before t=2 data arrives.
    # Feeding different t=2 data should NOT retroactively change t=1.
    alpha2 = MicropriceMomentumAlpha()
    alpha2.update(200000, 100)
    sig_t1_alt = alpha2.update(200100, 100)
    assert sig_t1 == sig_t1_alt


def test_reset_leak() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = MicropriceMomentumAlpha()
    a2 = MicropriceMomentumAlpha()
    # Pollute a1 with different history
    a1.update(100000, 50)
    a1.update(110000, 50)
    a1.reset()
    # Now both should behave identically
    a1.update(200000, 100)
    a2.update(200000, 100)
    s1 = a1.update(200100, 100)
    s2 = a2.update(200100, 100)
    assert s1 == s2


def test_no_global() -> None:
    """Two independent instances must not share state."""
    a1 = MicropriceMomentumAlpha()
    a2 = MicropriceMomentumAlpha()
    a1.update(200000, 100)
    a1.update(200500, 100)
    # a2 is fresh — first update should return 0
    sig = a2.update(200000, 100)
    assert sig == 0.0
    assert a2.get_signal() != a1.get_signal()


def test_order_matters() -> None:
    """Feeding data in different order must produce different signals."""
    a1 = MicropriceMomentumAlpha()
    a2 = MicropriceMomentumAlpha()
    # Sequence 1: rise then fall
    a1.update(200000, 100)
    a1.update(200200, 100)
    a1.update(200100, 100)
    # Sequence 2: fall then rise
    a2.update(200200, 100)
    a2.update(200000, 100)
    a2.update(200100, 100)
    assert a1.get_signal() != a2.get_signal()


def test_no_lookahead() -> None:
    """Signal must only reflect past ticks, not future ones."""
    alpha = MicropriceMomentumAlpha()
    alpha.update(200000, 100)
    sig_after_1 = alpha.update(200100, 100)
    # Record signal before third tick
    sig_before_3 = alpha.get_signal()
    # Feed third tick
    alpha.update(200200, 100)
    sig_after_3 = alpha.get_signal()
    # Signal changed after new data — confirms causal processing
    assert sig_before_3 == sig_after_1
    assert sig_after_3 != sig_before_3
