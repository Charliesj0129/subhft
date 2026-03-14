"""Gate B anti-leak / lookahead-bias tests for ToxicityAccelerationAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.toxicity_acceleration.impl import ToxicityAccelerationAlpha


def test_no_class_level_mutable_state() -> None:
    """ToxicityAccelerationAlpha should not have class-level mutable attributes."""
    assert hasattr(ToxicityAccelerationAlpha, "__slots__")
    for attr_name in ToxicityAccelerationAlpha.__slots__:
        assert not hasattr(ToxicityAccelerationAlpha, attr_name) or not isinstance(
            getattr(ToxicityAccelerationAlpha, attr_name, None), (list, dict, set)
        )


def test_reset_prevents_leakage() -> None:
    """After reset(), update gives same result as fresh instance."""
    a1 = ToxicityAccelerationAlpha()
    a2 = ToxicityAccelerationAlpha()
    # Pollute a1 with different data
    a1.update(800.0, 200.0, 100.0, 1.0)
    a1.update(200.0, 800.0, 50.0, -1.0)
    a1.reset()
    # Now both should behave identically
    s1 = a1.update(300.0, 300.0, 50.0, 0.5)
    s2 = a2.update(300.0, 300.0, 50.0, 0.5)
    assert s1 == pytest.approx(s2, abs=1e-9)


def test_update_order_matters() -> None:
    """Different orderings of the same data produce different signals."""
    a1 = ToxicityAccelerationAlpha()
    a2 = ToxicityAccelerationAlpha()
    # Establish non-zero toxicity baseline (slight imbalance) so ratio stays moderate
    for _ in range(100):
        a1.update(550.0, 450.0, 50.0, 0.5)
        a2.update(550.0, 450.0, 50.0, 0.5)
    # Sequence A: slightly more toxic then back to baseline
    for _ in range(20):
        a1.update(580.0, 420.0, 52.0, 0.5)
    s1 = a1.update(550.0, 450.0, 50.0, 0.5)
    # Sequence B: baseline then slightly more toxic
    for _ in range(20):
        a2.update(550.0, 450.0, 50.0, 0.5)
    s2 = a2.update(580.0, 420.0, 52.0, 0.5)
    # EMA state differs, so final signals differ
    assert s1 != pytest.approx(s2, abs=1e-6)


def test_no_future_data() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = ToxicityAccelerationAlpha()
    # Build up baseline then inject toxicity spike
    for _ in range(50):
        alpha.update(500.0, 500.0, 50.0, 0.5)
    for _ in range(10):
        sig1 = alpha.update(900.0, 100.0, 200.0, 1.0)  # strong positive flow
    sig2 = alpha.update(100.0, 500.0, 80.0, -1.0)  # reverse flow
    # Signal should change direction — proves no lookahead
    assert sig1 > 0.0
    assert sig2 < sig1


def test_two_instances_independent() -> None:
    """Two instances don't share state."""
    a1 = ToxicityAccelerationAlpha()
    a2 = ToxicityAccelerationAlpha()
    # Build up divergent state in a1
    for _ in range(50):
        a1.update(500.0, 500.0, 50.0, 1.0)
    for _ in range(10):
        a1.update(900.0, 100.0, 200.0, 1.0)
    # a2 gets its own divergent state
    for _ in range(50):
        a2.update(500.0, 500.0, 50.0, -1.0)
    for _ in range(10):
        s2 = a2.update(100.0, 900.0, 200.0, -1.0)
    # a2 should not be affected by a1
    assert s2 < 0.0
    assert a1.get_signal() > 0.0


def test_slots_enforced() -> None:
    """Cannot add arbitrary attributes to the instance."""
    alpha = ToxicityAccelerationAlpha()
    with pytest.raises(AttributeError):
        alpha.new_attr = 42  # type: ignore[attr-defined]


def test_deterministic() -> None:
    """Same inputs always produce same outputs."""
    signals_a: list[float] = []
    signals_b: list[float] = []
    for run_signals in (signals_a, signals_b):
        alpha = ToxicityAccelerationAlpha()
        for i in range(20):
            bid = 200.0 + i * 50.0
            ask = 300.0 - i * 20.0
            spread = 50.0 + i * 5.0
            ofi = 0.1 * (i - 10)
            sig = alpha.update(bid, ask, spread, ofi)
            run_signals.append(sig)
    for sa, sb in zip(signals_a, signals_b, strict=True):
        assert sa == pytest.approx(sb, abs=1e-12)
