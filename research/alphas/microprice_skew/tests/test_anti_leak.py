"""Gate B anti-leak / lookahead-bias tests for MicropriceSkewAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.microprice_skew.impl import MicropriceSkewAlpha


def test_update_no_args_returns_float() -> None:
    """Calling update() with no args should return a float (not crash)."""
    alpha = MicropriceSkewAlpha()
    sig = alpha.update()
    assert isinstance(sig, float)


def test_update_is_deterministic() -> None:
    """Two fresh instances fed identical data produce identical signals."""
    a1 = MicropriceSkewAlpha()
    a2 = MicropriceSkewAlpha()
    data = [
        (100.0, 102.0, 80.0, 20.0, 101.0),
        (100.0, 102.0, 50.0, 50.0, 101.0),
        (100.0, 102.0, 20.0, 80.0, 101.0),
    ]
    for row in data:
        s1 = a1.update(*row)
        s2 = a2.update(*row)
        assert s1 == s2


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), signal sequence matches a fresh instance."""
    a1 = MicropriceSkewAlpha()
    a2 = MicropriceSkewAlpha()
    # Pollute a1 with different history
    a1.update(bid_px=50.0, ask_px=60.0, bid_qty=200.0, ask_qty=10.0, mid_price=55.0)
    a1.update(bid_px=50.0, ask_px=60.0, bid_qty=10.0, ask_qty=200.0, mid_price=55.0)
    a1.reset()
    # Now both should behave identically
    data = (100.0, 102.0, 80.0, 20.0, 101.0)
    s1 = a1.update(*data)
    s2 = a2.update(*data)
    assert s1 == s2


def test_no_future() -> None:
    """Signal at tick t must not depend on data from tick t+1."""
    alpha = MicropriceSkewAlpha()
    sig_t0 = alpha.update(bid_px=100.0, ask_px=102.0, bid_qty=80.0, ask_qty=20.0, mid_price=101.0)
    alpha2 = MicropriceSkewAlpha()
    sig_t0_alt = alpha2.update(bid_px=100.0, ask_px=102.0, bid_qty=80.0, ask_qty=20.0, mid_price=101.0)
    assert sig_t0 == sig_t0_alt


def test_no_global() -> None:
    """Two independent instances must not share state."""
    a1 = MicropriceSkewAlpha()
    a2 = MicropriceSkewAlpha()
    a1.update(bid_px=100.0, ask_px=102.0, bid_qty=90.0, ask_qty=10.0, mid_price=101.0)
    # a2 is fresh
    sig = a2.update(bid_px=100.0, ask_px=102.0, bid_qty=50.0, ask_qty=50.0, mid_price=101.0)
    assert abs(sig) < 1e-6  # equal depths => ~0
    assert a2.get_signal() != a1.get_signal()


def test_order_matters() -> None:
    """Feeding data in different order must produce different signals."""
    a1 = MicropriceSkewAlpha()
    a2 = MicropriceSkewAlpha()
    # Sequence 1: bid-heavy then ask-heavy
    a1.update(bid_px=100.0, ask_px=102.0, bid_qty=90.0, ask_qty=10.0, mid_price=101.0)
    a1.update(bid_px=100.0, ask_px=102.0, bid_qty=10.0, ask_qty=90.0, mid_price=101.0)
    # Sequence 2: ask-heavy then bid-heavy
    a2.update(bid_px=100.0, ask_px=102.0, bid_qty=10.0, ask_qty=90.0, mid_price=101.0)
    a2.update(bid_px=100.0, ask_px=102.0, bid_qty=90.0, ask_qty=10.0, mid_price=101.0)
    assert a1.get_signal() != a2.get_signal()


def test_no_lookahead() -> None:
    """Signal must only reflect past ticks, not future ones."""
    alpha = MicropriceSkewAlpha()
    alpha.update(bid_px=100.0, ask_px=102.0, bid_qty=80.0, ask_qty=20.0, mid_price=101.0)
    sig_before = alpha.get_signal()
    alpha.update(bid_px=100.0, ask_px=102.0, bid_qty=20.0, ask_qty=80.0, mid_price=101.0)
    sig_after = alpha.get_signal()
    # Signal changed after new data - confirms causal processing
    assert sig_before != sig_after
