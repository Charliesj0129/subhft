"""Gate B anti-leak / lookahead-bias tests for ImbalanceDivergenceAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.imbalance_divergence.impl import ImbalanceDivergenceAlpha


def test_no_future_leak_signal_direction() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = ImbalanceDivergenceAlpha()
    sig1 = alpha.update(800_000, 200_000)  # l1 >> depth -> positive
    sig2 = alpha.update(200_000, 800_000)  # depth >> l1 -> should decrease
    assert sig1 > 0.0
    assert sig2 < sig1


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = ImbalanceDivergenceAlpha()
    a2 = ImbalanceDivergenceAlpha()
    a1.update(900_000, 100_000)
    a1.reset()
    s1 = a1.update(300_000, 300_000)
    s2 = a2.update(300_000, 300_000)
    assert s1 == s2


def test_no_global_state_between_instances() -> None:
    """Two independent instances must not share any mutable state."""
    a1 = ImbalanceDivergenceAlpha()
    a2 = ImbalanceDivergenceAlpha()
    a1.update(900_000, 100_000)
    assert a2.get_signal() == 0.0  # a2 untouched


def test_order_matters_no_commutativity() -> None:
    """Signal depends on the sequence of updates, not just the set of inputs."""
    a1 = ImbalanceDivergenceAlpha()
    a2 = ImbalanceDivergenceAlpha()
    # Feed same values in different order
    a1.update(800_000, 200_000)
    a1.update(200_000, 800_000)
    a2.update(200_000, 800_000)
    a2.update(800_000, 200_000)
    assert a1.get_signal() != a2.get_signal()


def test_no_lookahead_monotonic_ema() -> None:
    """Feeding a constant positive divergence should produce monotonically
    increasing signal when starting from a lower EMA state."""
    alpha = ImbalanceDivergenceAlpha()
    # Start with a negative divergence to set low EMA
    alpha.update(100_000, 900_000)
    prev = alpha.get_signal()
    # Now feed constant positive divergence — signal should rise each tick
    for _ in range(20):
        alpha.update(900_000, 100_000)
        current = alpha.get_signal()
        assert current >= prev
        prev = current
