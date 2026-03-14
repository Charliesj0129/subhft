"""Gate B anti-leak / lookahead-bias tests for AdverseFlowAsymmetryAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.adverse_flow_asymmetry.impl import AdverseFlowAsymmetryAlpha


def test_no_class_level_mutable_state() -> None:
    """AdverseFlowAsymmetryAlpha should not have class-level mutable attributes."""
    assert hasattr(AdverseFlowAsymmetryAlpha, "__slots__")
    for attr_name in AdverseFlowAsymmetryAlpha.__slots__:
        assert not hasattr(AdverseFlowAsymmetryAlpha, attr_name) or not isinstance(
            getattr(AdverseFlowAsymmetryAlpha, attr_name, None), (list, dict, set)
        )


def test_reset_prevents_leakage() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = AdverseFlowAsymmetryAlpha()
    a2 = AdverseFlowAsymmetryAlpha()
    # Pollute a1 with different data
    a1.update(800.0, 200.0)
    a1.update(200.0, 800.0)
    a1.reset()
    # Now both should behave identically
    s1 = a1.update(300.0, 300.0)
    s2 = a2.update(300.0, 300.0)
    assert s1 == pytest.approx(s2, abs=1e-9)


def test_update_order_matters() -> None:
    """Different orderings of the same data produce different signals."""
    a1 = AdverseFlowAsymmetryAlpha()
    a2 = AdverseFlowAsymmetryAlpha()
    # Sequence A: positive flow then negative
    a1.update(800.0, 100.0)
    s1 = a1.update(100.0, 800.0)
    # Sequence B: negative flow then positive
    a2.update(100.0, 800.0)
    s2 = a2.update(800.0, 100.0)
    # EMA state differs, so final signals differ
    assert s1 != pytest.approx(s2, abs=1e-6)


def test_no_future_data() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = AdverseFlowAsymmetryAlpha()
    sig1 = alpha.update(800.0, 100.0)  # strong positive flow
    sig2 = alpha.update(100.0, 800.0)  # reverse flow
    # Signal should change direction — proves no lookahead
    assert sig1 > 0.0
    assert sig2 < sig1


def test_two_instances_independent() -> None:
    """Two instances are completely independent."""
    a1 = AdverseFlowAsymmetryAlpha()
    a2 = AdverseFlowAsymmetryAlpha()
    a1.update(900.0, 100.0)
    a1.update(900.0, 100.0)
    s2 = a2.update(100.0, 900.0)
    # a2 should not be affected by a1
    assert s2 < 0.0
    assert a1.get_signal() > 0.0


def test_slots_enforced() -> None:
    """Cannot add arbitrary attributes (slots enforcement)."""
    alpha = AdverseFlowAsymmetryAlpha()
    with pytest.raises(AttributeError):
        alpha.some_random_attr = 42  # type: ignore[attr-defined]


def test_deterministic() -> None:
    """Same input sequence always produces same output sequence."""
    signals_a: list[float] = []
    signals_b: list[float] = []
    inputs = [(200.0 + i * 50.0, 300.0 - i * 20.0) for i in range(10)]
    a1 = AdverseFlowAsymmetryAlpha()
    a2 = AdverseFlowAsymmetryAlpha()
    for bid, ask in inputs:
        signals_a.append(a1.update(bid, ask))
    for bid, ask in inputs:
        signals_b.append(a2.update(bid, ask))
    for sa, sb in zip(signals_a, signals_b):
        assert sa == pytest.approx(sb, abs=1e-12)
