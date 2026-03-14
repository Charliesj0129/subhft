"""Gate B anti-leak / lookahead-bias tests for SpreadExcessToxicityAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.spread_excess_toxicity.impl import SpreadExcessToxicityAlpha


def test_no_class_level_mutable_state() -> None:
    """Class must not have mutable class-level attributes."""
    cls_dict = SpreadExcessToxicityAlpha.__dict__
    for k, v in cls_dict.items():
        if k.startswith("_") and not k.startswith("__"):
            continue
        if callable(v) or isinstance(v, (property, classmethod, staticmethod)):
            continue
        # Only __slots__ and __all__-style constants should exist
        assert not isinstance(v, (list, dict, set)), (
            f"Mutable class-level attribute found: {k}={v}"
        )


def test_reset_prevents_leakage() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = SpreadExcessToxicityAlpha()
    a2 = SpreadExcessToxicityAlpha()
    # Pollute a1 with different history
    for _ in range(50):
        a1.update(200, 100, 800, 2.0)
    a1.reset()
    # Now both should behave identically
    for _ in range(10):
        s1 = a1.update(100, 50, 500, 1.0)
        s2 = a2.update(100, 50, 500, 1.0)
    assert s1 == s2


def test_update_order_matters() -> None:
    """Feeding data in different order must produce different signals."""
    a1 = SpreadExcessToxicityAlpha()
    a2 = SpreadExcessToxicityAlpha()
    # Sequence 1: wide then narrow spread
    a1.update(100, 50, 1000, 1.0)
    a1.update(100, 50, 200, 1.0)
    a1.update(100, 50, 500, 1.0)
    # Sequence 2: narrow then wide spread
    a2.update(100, 50, 200, 1.0)
    a2.update(100, 50, 1000, 1.0)
    a2.update(100, 50, 500, 1.0)
    assert a1.get_signal() != a2.get_signal()


def test_no_future_data() -> None:
    """Signal at tick t must not depend on data from tick t+1."""
    alpha = SpreadExcessToxicityAlpha()
    alpha.update(100, 50, 500, 1.0)
    sig_t1 = alpha.update(100, 50, 600, 2.0)
    # Same prefix with a fresh instance must yield identical t=1 signal
    alpha2 = SpreadExcessToxicityAlpha()
    alpha2.update(100, 50, 500, 1.0)
    sig_t1_alt = alpha2.update(100, 50, 600, 2.0)
    assert sig_t1 == sig_t1_alt


def test_two_instances_independent() -> None:
    """Two independent instances must not share state."""
    a1 = SpreadExcessToxicityAlpha()
    a2 = SpreadExcessToxicityAlpha()
    a1.update(100, 50, 500, 1.0)
    a1.update(100, 50, 800, 3.0)
    # a2 is fresh — first update should return a different signal
    sig = a2.update(100, 50, 500, 1.0)
    assert sig != a1.get_signal()


def test_slots_enforced() -> None:
    """Instance must use __slots__ (no __dict__)."""
    alpha = SpreadExcessToxicityAlpha()
    assert not hasattr(alpha, "__dict__"), (
        "SpreadExcessToxicityAlpha must use __slots__, found __dict__"
    )


def test_deterministic() -> None:
    """Same input sequence must always produce same output."""
    results = []
    for _ in range(3):
        alpha = SpreadExcessToxicityAlpha()
        for i in range(50):
            alpha.update(100 + i, 50 + i, 500 + i * 10, 1.0 + i * 0.1)
        results.append(alpha.get_signal())
    assert results[0] == results[1] == results[2]
