"""Gate B anti-leak / lookahead-bias tests for ToxicityMultiscaleAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.toxicity_multiscale.impl import ToxicityMultiscaleAlpha


def test_no_future_data_access() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = ToxicityMultiscaleAlpha()
    alpha.update(500.0, 100.0, 80.0, 100.0)  # init tick
    sig1 = alpha.update(500.0, 100.0, 80.0, 101.0)  # positive QI
    sig2 = alpha.update(100.0, 500.0, 80.0, 102.0)  # negative QI
    # Signal should change direction
    assert sig1 >= 0.0
    assert sig2 < sig1


def test_reset_prevents_leakage() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = ToxicityMultiscaleAlpha()
    a2 = ToxicityMultiscaleAlpha()
    # Pollute a1 with different data
    a1.update(800.0, 200.0, 100.0, 100.0)
    a1.update(200.0, 800.0, 50.0, 105.0)
    a1.reset()
    # Now both should behave identically
    s1 = a1.update(300.0, 300.0, 50.0, 100.0)
    s2 = a2.update(300.0, 300.0, 50.0, 100.0)
    assert s1 == pytest.approx(s2, abs=1e-9)


def test_no_global_state() -> None:
    """Two instances are completely independent."""
    a1 = ToxicityMultiscaleAlpha()
    a2 = ToxicityMultiscaleAlpha()
    a1.update(900.0, 100.0, 200.0, 100.0)
    a1.update(900.0, 100.0, 200.0, 105.0)
    a2.update(100.0, 900.0, 50.0, 100.0)
    s2 = a2.update(100.0, 900.0, 50.0, 105.0)
    # a2 should not be affected by a1
    assert s2 < 0.0
    assert a1.get_signal() > 0.0


def test_update_order_matters() -> None:
    """Different orderings of the same data produce different signals."""
    a1 = ToxicityMultiscaleAlpha()
    a2 = ToxicityMultiscaleAlpha()
    # Sequence A
    a1.update(800.0, 100.0, 100.0, 100.0)
    s1 = a1.update(100.0, 800.0, 100.0, 105.0)
    # Sequence B (reversed)
    a2.update(100.0, 800.0, 100.0, 100.0)
    s2 = a2.update(800.0, 100.0, 100.0, 105.0)
    # EMA state differs, so final signals differ
    assert s1 != pytest.approx(s2, abs=1e-6)


def test_no_lookahead_in_ema() -> None:
    """EMA update uses only current and past values, never future."""
    alpha = ToxicityMultiscaleAlpha()
    signals: list[float] = []
    for i in range(10):
        bid = 200.0 + i * 50.0
        ask = 300.0 - i * 20.0
        spread = 50.0 + i * 5.0
        mid = 100.0 + i * 0.5
        sig = alpha.update(bid, ask, spread, mid)
        signals.append(sig)
    # Replay and verify identical results
    alpha2 = ToxicityMultiscaleAlpha()
    for i in range(10):
        bid = 200.0 + i * 50.0
        ask = 300.0 - i * 20.0
        spread = 50.0 + i * 5.0
        mid = 100.0 + i * 0.5
        sig2 = alpha2.update(bid, ask, spread, mid)
        assert sig2 == pytest.approx(signals[i], abs=1e-12)


def test_no_class_level_mutable_state() -> None:
    """ToxicityMultiscaleAlpha should not have class-level mutable attributes."""
    assert hasattr(ToxicityMultiscaleAlpha, "__slots__")
    for attr_name in ToxicityMultiscaleAlpha.__slots__:
        assert not hasattr(ToxicityMultiscaleAlpha, attr_name) or not isinstance(
            getattr(ToxicityMultiscaleAlpha, attr_name, None), (list, dict, set)
        )
