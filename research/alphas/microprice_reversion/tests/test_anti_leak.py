"""Gate B anti-leak / lookahead-bias tests for MicropriceReversionAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.microprice_reversion.impl import MicropriceReversionAlpha


def test_no_future_data_access() -> None:
    """Signal at tick t only uses data up to tick t."""
    alpha = MicropriceReversionAlpha()
    sig1 = alpha.update(110, 100, 10)
    # Signal should only reflect data seen so far
    sig2 = alpha.update(90, 100, 10)
    # sig2 should differ from sig1 (it incorporated new data)
    assert sig1 != sig2
    # After positive dev then negative dev, signal should move toward positive
    assert sig2 > sig1


def test_reset_prevents_leakage() -> None:
    """After reset, old data doesn't affect new signal."""
    a1 = MicropriceReversionAlpha()
    a2 = MicropriceReversionAlpha()
    # Feed a1 some history, then reset
    a1.update(200, 100, 10)
    a1.update(300, 100, 10)
    a1.reset()
    # Both should produce identical results from the same input
    s1 = a1.update(110, 100, 10)
    s2 = a2.update(110, 100, 10)
    assert s1 == s2


def test_no_global_state() -> None:
    """Two independent instances don't share state."""
    a1 = MicropriceReversionAlpha()
    a2 = MicropriceReversionAlpha()
    a1.update(200, 100, 10)
    # a2 should still have zero signal
    assert a2.get_signal() == 0.0
    # a2 fed different data should produce different signal
    s2 = a2.update(90, 100, 10)
    assert s2 != a1.get_signal()


def test_update_order_matters() -> None:
    """Different tick sequences produce different signals."""
    a1 = MicropriceReversionAlpha()
    a2 = MicropriceReversionAlpha()
    # Sequence 1: up then down
    a1.update(120, 100, 10)
    a1.update(80, 100, 10)
    # Sequence 2: down then up
    a2.update(80, 100, 10)
    a2.update(120, 100, 10)
    # Final signals should differ due to EMA memory
    assert a1.get_signal() != a2.get_signal()


def test_no_lookahead_in_ema() -> None:
    """EMA only uses current and past values.

    Verify by checking that signal after N ticks equals manual EMA computation.
    """
    alpha = MicropriceReversionAlpha()
    from research.alphas.microprice_reversion.impl import _EMA_ALPHA

    data = [(110, 100, 10), (90, 100, 10), (105, 100, 10)]
    ema = None
    for microprice_x2, mid_price_x2, spread_scaled in data:
        dev = (microprice_x2 - mid_price_x2) / max(spread_scaled, 1)
        if ema is None:
            ema = dev
        else:
            ema = ema + _EMA_ALPHA * (dev - ema)
        sig = alpha.update(microprice_x2, mid_price_x2, spread_scaled)
        assert sig == -ema


def test_no_class_level_mutable_state() -> None:
    """Ensure no class-level mutable state that could leak between instances."""
    a1 = MicropriceReversionAlpha()
    a2 = MicropriceReversionAlpha()
    # Verify __slots__ is used (no __dict__)
    assert not hasattr(a1, "__dict__")
    # Modify a1, check a2 unaffected
    a1.update(200, 100, 5)
    assert a2.get_signal() == 0.0
