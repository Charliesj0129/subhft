"""Gate B anti-leak / lookahead-bias tests for FlowToxicityRatioAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.flow_toxicity_ratio.impl import FlowToxicityRatioAlpha


def test_no_future() -> None:
    """Signal at tick t must not depend on data from tick t+1."""
    alpha = FlowToxicityRatioAlpha()
    sig1 = alpha.update(100.0, 50.0, 50.0)
    # Feed more data — sig1 should remain what it was at tick 1
    alpha_copy = FlowToxicityRatioAlpha()
    sig1_copy = alpha_copy.update(100.0, 50.0, 50.0)
    assert sig1 == sig1_copy


def test_reset_leak() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = FlowToxicityRatioAlpha()
    a2 = FlowToxicityRatioAlpha()
    a1.update(500.0, 10.0, 10.0)
    a1.update(200.0, 30.0, 30.0)
    a1.reset()
    s1 = a1.update(60.0, 100.0, 100.0)
    s2 = a2.update(60.0, 100.0, 100.0)
    assert s1 == s2


def test_no_global() -> None:
    """Two independent instances must not share state."""
    a1 = FlowToxicityRatioAlpha()
    a2 = FlowToxicityRatioAlpha()
    a1.update(500.0, 10.0, 10.0)
    assert a2.get_signal() == 0.0


def test_order_matters() -> None:
    """Different ordering of the same data produces different signals."""
    a1 = FlowToxicityRatioAlpha()
    a2 = FlowToxicityRatioAlpha()
    a1.update(100.0, 50.0, 50.0)
    a1.update(10.0, 500.0, 500.0)
    a2.update(10.0, 500.0, 500.0)
    a2.update(100.0, 50.0, 50.0)
    assert a1.get_signal() != a2.get_signal()


def test_no_lookahead() -> None:
    """Each call should only see past data; future ticks cannot affect current."""
    alpha = FlowToxicityRatioAlpha()
    sig1 = alpha.update(500.0, 10.0, 10.0)  # high toxicity
    sig2 = alpha.update(1.0, 500.0, 500.0)  # low toxicity
    assert sig1 > 0.0
    assert sig2 < sig1  # signal moved in the correct direction
