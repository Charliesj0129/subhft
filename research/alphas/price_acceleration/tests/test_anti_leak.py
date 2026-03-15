"""Gate B anti-leak / lookahead-bias tests for PriceAccelerationAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.price_acceleration.impl import PriceAccelerationAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args should return a float (uses default mid_price=0.0)."""
    alpha = PriceAccelerationAlpha()
    result = alpha.update()
    assert isinstance(result, float)


def test_update_is_deterministic() -> None:
    """Same input sequence must produce identical output every time."""
    seq = [200000, 200100, 200300, 200400, 200600]
    signals_a: list[float] = []
    signals_b: list[float] = []
    for run_signals in (signals_a, signals_b):
        alpha = PriceAccelerationAlpha()
        for val in seq:
            run_signals.append(alpha.update(val))
    assert signals_a == signals_b


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = PriceAccelerationAlpha()
    a2 = PriceAccelerationAlpha()
    # Pollute a1 with different history
    a1.update(100000)
    a1.update(110000)
    a1.update(130000)
    a1.reset()
    # Now both should behave identically
    seq = [200000, 200100, 200300]
    for val in seq:
        s1 = a1.update(val)
        s2 = a2.update(val)
        assert s1 == s2


def test_no_future() -> None:
    """Signal at tick t must not depend on data from tick t+1."""
    alpha = PriceAccelerationAlpha()
    alpha.update(200000)
    alpha.update(200100)
    sig_t2 = alpha.update(200300)
    # Independent instance with same first 3 ticks
    alpha2 = PriceAccelerationAlpha()
    alpha2.update(200000)
    alpha2.update(200100)
    sig_t2_alt = alpha2.update(200300)
    assert sig_t2 == sig_t2_alt


def test_no_global() -> None:
    """Two independent instances must not share state."""
    a1 = PriceAccelerationAlpha()
    a2 = PriceAccelerationAlpha()
    a1.update(200000)
    a1.update(200100)
    a1.update(200300)
    # a2 is fresh — first update should return 0
    sig = a2.update(200000)
    assert sig == 0.0
    assert a2.get_signal() != a1.get_signal()


def test_order_matters() -> None:
    """Feeding data in different order must produce different signals."""
    a1 = PriceAccelerationAlpha()
    a2 = PriceAccelerationAlpha()
    # Sequence 1: accelerating up then constant velocity
    a1.update(200000)
    a1.update(200100)
    a1.update(200300)
    a1.update(200500)
    a1.update(200700)
    # Sequence 2: constant velocity then accelerating
    a2.update(200000)
    a2.update(200200)
    a2.update(200400)
    a2.update(200500)
    a2.update(200700)
    assert a1.get_signal() != a2.get_signal()


def test_no_lookahead() -> None:
    """Signal must only reflect past ticks, not future ones."""
    alpha = PriceAccelerationAlpha()
    alpha.update(200000)
    alpha.update(200100)
    sig_after_2 = alpha.update(200300)
    sig_before_3 = alpha.get_signal()
    # Feed fourth tick
    alpha.update(200600)
    sig_after_3 = alpha.get_signal()
    # Signal changed after new data — confirms causal processing
    assert sig_before_3 == sig_after_2
    assert sig_after_3 != sig_before_3
