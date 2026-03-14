"""Gate B anti-leak / lookahead-bias tests for ToxicityTimescaleDivergenceAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.toxicity_timescale_divergence.impl import (
    ToxicityTimescaleDivergenceAlpha,
)


def test_no_class_level_mutable_state() -> None:
    """No class-level mutable attributes; all state in __slots__."""
    assert hasattr(ToxicityTimescaleDivergenceAlpha, "__slots__")
    for attr_name in ToxicityTimescaleDivergenceAlpha.__slots__:
        assert not hasattr(
            ToxicityTimescaleDivergenceAlpha, attr_name
        ) or not isinstance(
            getattr(ToxicityTimescaleDivergenceAlpha, attr_name, None),
            (list, dict, set),
        )


def test_reset_prevents_leakage() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = ToxicityTimescaleDivergenceAlpha()
    a2 = ToxicityTimescaleDivergenceAlpha()
    # Pollute a1 with different data
    a1.update(800.0, 200.0, 100.0)
    a1.update(200.0, 800.0, 50.0)
    a1.reset()
    # Now both should behave identically
    s1 = a1.update(300.0, 300.0, 50.0)
    s2 = a2.update(300.0, 300.0, 50.0)
    assert s1 == pytest.approx(s2, abs=1e-9)


def test_update_order_matters() -> None:
    """Different orderings of the same data produce different signals."""
    a1 = ToxicityTimescaleDivergenceAlpha()
    a2 = ToxicityTimescaleDivergenceAlpha()
    # Sequence A: positive qi then negative
    a1.update(800.0, 100.0, 100.0)
    s1 = a1.update(100.0, 800.0, 100.0)
    # Sequence B: negative qi then positive
    a2.update(100.0, 800.0, 100.0)
    s2 = a2.update(800.0, 100.0, 100.0)
    # EMA state differs, so final signals differ
    assert s1 != pytest.approx(s2, abs=1e-6)


def test_no_future_data() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = ToxicityTimescaleDivergenceAlpha()
    # Establish baseline
    for _ in range(20):
        alpha.update(500.0, 500.0, 50.0)
    sig1 = alpha.update(900.0, 100.0, 50.0)  # strong positive
    sig2 = alpha.update(100.0, 900.0, 50.0)  # reverse
    # Signal should change — proves no lookahead
    assert sig1 > sig2


def test_two_instances_independent() -> None:
    """Two instances are completely independent."""
    a1 = ToxicityTimescaleDivergenceAlpha()
    a2 = ToxicityTimescaleDivergenceAlpha()
    # Build up positive divergence in a1 (balanced -> strong bid)
    for _ in range(10):
        a1.update(500.0, 500.0, 200.0)
    for _ in range(5):
        a1.update(900.0, 100.0, 200.0)
    # a2 gets negative divergence (balanced -> strong ask)
    for _ in range(10):
        a2.update(500.0, 500.0, 50.0)
    for _ in range(5):
        s2 = a2.update(100.0, 900.0, 50.0)
    # a2 should not be affected by a1
    assert a1.get_signal() > 0.0
    assert s2 < 0.0


def test_slots_enforced() -> None:
    """Cannot add arbitrary attributes (enforced by __slots__)."""
    alpha = ToxicityTimescaleDivergenceAlpha()
    with pytest.raises(AttributeError):
        alpha.random_attr = 42  # type: ignore[attr-defined]


def test_deterministic() -> None:
    """Same input sequence always produces same output."""
    signals_a: list[float] = []
    signals_b: list[float] = []
    for run_signals in (signals_a, signals_b):
        alpha = ToxicityTimescaleDivergenceAlpha()
        for i in range(20):
            bid = 200.0 + i * 50.0
            ask = 300.0 - i * 10.0
            spread = 50.0 + i * 5.0
            sig = alpha.update(bid, ask, spread)
            run_signals.append(sig)
    for sa, sb in zip(signals_a, signals_b, strict=True):
        assert sa == pytest.approx(sb, abs=1e-12)
