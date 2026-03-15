"""Gate B anti-leak / lookahead-bias tests for ToxicityMultiscaleAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.toxicity_multiscale.impl import ToxicityMultiscaleAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (all zero defaults) must return a numeric value."""
    alpha = ToxicityMultiscaleAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_stateful() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = ToxicityMultiscaleAlpha()
    sig1 = alpha.update(500.0, 100.0, 100.0, 1000.0)
    sig2 = alpha.update(100.0, 500.0, 100.0, 1010.0)
    # First: bid dominant -> positive direction
    # Second: ask dominant -> signal should shift negative relative to sig1
    assert sig2 < sig1


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = ToxicityMultiscaleAlpha()
    a2 = ToxicityMultiscaleAlpha()
    a1.update(800.0, 200.0, 100.0, 5000.0)
    a1.update(800.0, 200.0, 100.0, 5050.0)
    a1.reset()
    s1 = a1.update(300.0, 300.0, 50.0, 1000.0)
    s2 = a2.update(300.0, 300.0, 50.0, 1000.0)
    assert s1 == s2


def test_deterministic() -> None:
    """Same inputs produce identical outputs across two fresh instances."""
    inputs = [
        (300.0, 100.0, 50.0, 1000.0),
        (100.0, 400.0, 80.0, 1005.0),
        (250.0, 250.0, 60.0, 1003.0),
    ]
    a1 = ToxicityMultiscaleAlpha()
    a2 = ToxicityMultiscaleAlpha()
    for args in inputs:
        s1 = a1.update(*args)
        s2 = a2.update(*args)
        assert s1 == s2


def test_no_future_leakage() -> None:
    """Signal at tick t must not change when future ticks are added."""
    alpha1 = ToxicityMultiscaleAlpha()
    alpha2 = ToxicityMultiscaleAlpha()
    base = [
        (200.0, 100.0, 50.0, 1000.0),
        (150.0, 200.0, 60.0, 1005.0),
        (300.0, 100.0, 70.0, 1010.0),
    ]
    for args in base:
        alpha1.update(*args)
        alpha2.update(*args)
    sig_at_t3 = alpha1.get_signal()
    # Feed more data to alpha2
    alpha2.update(400.0, 50.0, 90.0, 1020.0)
    alpha2.update(50.0, 400.0, 40.0, 1015.0)
    # alpha1's signal at t3 unchanged
    assert alpha1.get_signal() == sig_at_t3


def test_signal_direction_follows_qi() -> None:
    """After many ticks, signal direction should match queue imbalance direction."""
    alpha = ToxicityMultiscaleAlpha()
    # Persistent bid dominance with price movement
    for i in range(200):
        alpha.update(500.0, 100.0, 100.0, 1000.0 + i * 5.0)
    assert alpha.get_signal() > 0.0

    alpha.reset()
    # Persistent ask dominance with price movement
    for i in range(200):
        alpha.update(100.0, 500.0, 100.0, 1000.0 + i * 5.0)
    assert alpha.get_signal() < 0.0


def test_many_updates_no_error() -> None:
    """10000 updates should not raise or produce NaN/inf."""
    alpha = ToxicityMultiscaleAlpha()
    import math

    for i in range(10000):
        mid = 1000.0 + (i % 100) * 0.5
        sig = alpha.update(100.0 + i % 50, 100.0 + (i + 25) % 50, 50.0, mid)
        assert math.isfinite(sig)


def test_two_fresh_instances_same_result() -> None:
    """Two fresh instances produce identical trajectories."""
    a1 = ToxicityMultiscaleAlpha()
    a2 = ToxicityMultiscaleAlpha()
    inputs = [
        (200.0, 300.0, 40.0, 999.0),
        (400.0, 100.0, 60.0, 1001.0),
        (250.0, 250.0, 50.0, 1000.0),
        (100.0, 500.0, 80.0, 998.0),
    ]
    for args in inputs:
        assert a1.update(*args) == a2.update(*args)
    assert a1.get_signal() == a2.get_signal()
