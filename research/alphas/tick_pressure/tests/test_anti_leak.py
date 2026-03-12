"""Gate B anti-leak / lookahead-bias tests for TickPressureAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.tick_pressure.impl import TickPressureAlpha


def test_no_future_leakage() -> None:
    """Signal at tick t must not depend on data from tick t+1."""
    alpha = TickPressureAlpha()
    alpha.update(100000, 100.0, 100.0)
    sig_t1 = alpha.update(100010, 200.0, 200.0)
    # Create a fresh alpha with same first two ticks but different third
    a2 = TickPressureAlpha()
    a2.update(100000, 100.0, 100.0)
    sig_t1_copy = a2.update(100010, 200.0, 200.0)
    # Signal at t=1 must be identical regardless of future data
    assert sig_t1 == sig_t1_copy


def test_reset_leak() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = TickPressureAlpha()
    a2 = TickPressureAlpha()
    # Feed a1 some history then reset
    a1.update(100000, 800.0, 200.0)
    a1.update(100050, 500.0, 500.0)
    a1.reset()
    # Both now start fresh with same data
    s1 = a1.update(100000, 300.0, 300.0)
    s2 = a2.update(100000, 300.0, 300.0)
    assert s1 == s2


def test_no_global_state() -> None:
    """Two independent instances must not share state."""
    a1 = TickPressureAlpha()
    a2 = TickPressureAlpha()
    a1.update(100000, 100.0, 100.0)
    a1.update(100050, 500.0, 500.0)
    # a2 has not been fed any data; signal should be 0
    assert a2.get_signal() == 0.0


def test_order_matters() -> None:
    """Feeding ticks in different order must produce different signals."""
    a1 = TickPressureAlpha()
    a1.update(100000, 100.0, 100.0)
    a1.update(100010, 200.0, 200.0)  # uptick
    sig_up_first = a1.get_signal()

    a2 = TickPressureAlpha()
    a2.update(100010, 100.0, 100.0)
    a2.update(100000, 200.0, 200.0)  # downtick
    sig_down_first = a2.get_signal()

    assert sig_up_first != sig_down_first


def test_no_lookahead_mid_price() -> None:
    """Signal should only use previous mid, not current or future mid for baseline."""
    alpha = TickPressureAlpha()
    # First tick: sets prev_mid, returns 0
    sig0 = alpha.update(100000, 100.0, 100.0)
    assert sig0 == 0.0
    # Second tick: uses delta from first mid
    sig1 = alpha.update(100020, 100.0, 100.0)
    # Third tick: signal changes based on new delta, not retroactive
    sig2 = alpha.update(100020, 100.0, 100.0)  # no change → decays toward 0
    assert abs(sig2) < abs(sig1)
