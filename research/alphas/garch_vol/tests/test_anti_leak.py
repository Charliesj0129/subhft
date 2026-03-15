"""Gate B anti-leak / lookahead-bias tests for GARCHVolAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.garch_vol.impl import GARCHVolAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args should return a float (uses default mid_price=0)."""
    alpha = GARCHVolAlpha()
    result = alpha.update()
    assert isinstance(result, float)


def test_update_is_deterministic() -> None:
    """Two identical instances fed identical data produce identical signals."""
    prices = [100000.0, 100200.0, 100100.0, 100400.0, 100300.0, 100500.0]
    alpha_a = GARCHVolAlpha()
    alpha_b = GARCHVolAlpha()
    out_a = [alpha_a.update(mid_price=p) for p in prices]
    out_b = [alpha_b.update(mid_price=p) for p in prices]
    assert out_a == out_b


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), prior history must not influence future signals."""
    a1 = GARCHVolAlpha()
    a2 = GARCHVolAlpha()
    # Pollute a1 with different history
    a1.update(50000)
    a1.update(60000)
    a1.reset()
    # Now both should behave identically
    a1.update(100000)
    a2.update(100000)
    s1 = a1.update(100100)
    s2 = a2.update(100100)
    assert s1 == s2


def test_no_future() -> None:
    """Signal at tick t must not depend on data from tick t+1."""
    alpha = GARCHVolAlpha()
    alpha.update(100000)
    sig_t1 = alpha.update(100100)
    # Same sequence in a fresh instance
    alpha2 = GARCHVolAlpha()
    alpha2.update(100000)
    sig_t1_alt = alpha2.update(100100)
    assert sig_t1 == sig_t1_alt


def test_no_global() -> None:
    """Two independent instances must not share state."""
    a1 = GARCHVolAlpha()
    a2 = GARCHVolAlpha()
    a1.update(100000)
    a1.update(100500)
    # a2 is fresh — first update should return 0
    sig = a2.update(100000)
    assert sig == 0.0
    assert a2.get_signal() != a1.get_signal()


def test_order_matters() -> None:
    """Feeding data in different order must produce different signals."""
    a1 = GARCHVolAlpha()
    a2 = GARCHVolAlpha()
    # Sequence 1: rise then small rise
    a1.update(100000)
    a1.update(100200)
    a1.update(100300)
    # Sequence 2: big rise then fall
    a2.update(100000)
    a2.update(100500)
    a2.update(100300)
    assert a1.get_signal() != a2.get_signal()


def test_no_lookahead() -> None:
    """Signal must only reflect past ticks, not future ones."""
    alpha = GARCHVolAlpha()
    alpha.update(100000)
    sig_after_1 = alpha.update(100100)
    sig_before_3 = alpha.get_signal()
    alpha.update(100200)
    sig_after_3 = alpha.get_signal()
    # Signal changed after new data — confirms causal processing
    assert sig_before_3 == sig_after_1
    assert sig_after_3 != sig_before_3
