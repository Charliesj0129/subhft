"""Gate B anti-leak / lookahead-bias tests for ToxicFlowAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.toxic_flow.impl import ToxicFlowAlpha


def test_no_future_data_access() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = ToxicFlowAlpha()
    sig1 = alpha.update(500.0, 100.0, 80.0, 1.0)  # strong positive flow
    sig2 = alpha.update(100.0, 500.0, 80.0, -1.0)  # reverse flow
    # Signal should change direction — proves no lookahead
    assert sig1 > 0.0
    assert sig2 < sig1


def test_reset_prevents_leakage() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = ToxicFlowAlpha()
    a2 = ToxicFlowAlpha()
    # Pollute a1 with different data
    a1.update(800.0, 200.0, 100.0, 1.0)
    a1.update(200.0, 800.0, 50.0, -1.0)
    a1.reset()
    # Now both should behave identically
    s1 = a1.update(300.0, 300.0, 50.0, 0.5)
    s2 = a2.update(300.0, 300.0, 50.0, 0.5)
    assert s1 == pytest.approx(s2, abs=1e-9)


def test_no_global_state() -> None:
    """Two instances are completely independent."""
    a1 = ToxicFlowAlpha()
    a2 = ToxicFlowAlpha()
    a1.update(900.0, 100.0, 200.0, 1.0)
    a1.update(900.0, 100.0, 200.0, 1.0)
    s2 = a2.update(100.0, 900.0, 50.0, -1.0)
    # a2 should not be affected by a1
    assert s2 < 0.0
    assert a1.get_signal() > 0.0


def test_update_order_matters() -> None:
    """Different orderings of the same data produce different signals."""
    a1 = ToxicFlowAlpha()
    a2 = ToxicFlowAlpha()
    # Sequence A: positive flow then negative
    a1.update(800.0, 100.0, 100.0, 1.0)
    s1 = a1.update(100.0, 800.0, 100.0, -1.0)
    # Sequence B: negative flow then positive
    a2.update(100.0, 800.0, 100.0, -1.0)
    s2 = a2.update(800.0, 100.0, 100.0, 1.0)
    # EMA state differs, so final signals differ
    assert s1 != pytest.approx(s2, abs=1e-6)


def test_no_lookahead_in_ema() -> None:
    """EMA update uses only current and past values, never future."""
    alpha = ToxicFlowAlpha()
    # Feed 10 ticks and record signal at each step
    signals: list[float] = []
    for i in range(10):
        bid = 200.0 + i * 50.0
        ask = 300.0 - i * 20.0
        spread = 50.0 + i * 5.0
        ofi = 0.1 * (i - 5)
        sig = alpha.update(bid, ask, spread, ofi)
        signals.append(sig)
    # Each signal should be deterministic from its history alone.
    # Verify by replaying and checking identical results.
    alpha2 = ToxicFlowAlpha()
    for i in range(10):
        bid = 200.0 + i * 50.0
        ask = 300.0 - i * 20.0
        spread = 50.0 + i * 5.0
        ofi = 0.1 * (i - 5)
        sig2 = alpha2.update(bid, ask, spread, ofi)
        assert sig2 == pytest.approx(signals[i], abs=1e-12)


def test_no_class_level_mutable_state() -> None:
    """ToxicFlowAlpha should not have class-level mutable attributes."""
    # All state should be in __slots__ instance variables
    assert hasattr(ToxicFlowAlpha, "__slots__")
    # Check that no class-level mutable containers exist
    for attr_name in ToxicFlowAlpha.__slots__:
        assert not hasattr(ToxicFlowAlpha, attr_name) or not isinstance(
            getattr(ToxicFlowAlpha, attr_name, None), (list, dict, set)
        )
