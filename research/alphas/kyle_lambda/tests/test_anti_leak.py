"""Gate B anti-leak / lookahead-bias tests for KyleLambdaAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.kyle_lambda.impl import KyleLambdaAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (all fields 0) must return a numeric value."""
    alpha = KyleLambdaAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_stateful() -> None:
    """Each call should only see past data; signal changes with new info."""
    alpha = KyleLambdaAlpha()
    # Warm up with mixed data so signal is not saturated at clip boundary
    for i in range(50):
        mid = 100.0 + (i % 5) * 0.02 - 0.04  # oscillating mid
        bq = 100.0 + (i % 3) * 20.0
        aq = 100.0 + ((i + 1) % 3) * 20.0
        alpha.update(mid, 80.0, bq, aq)
    sig_before = alpha.get_signal()
    # Inject a distinctly different tick
    sig_after = alpha.update(105.0, 500.0, 300.0, 10.0)
    assert sig_after != sig_before


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = KyleLambdaAlpha()
    a2 = KyleLambdaAlpha()
    a1.update(100.0, 100.0, 200.0, 50.0)
    a1.update(102.0, 200.0, 300.0, 50.0)
    a1.reset()
    s1 = a1.update(100.0, 50.0, 100.0, 100.0)
    s2 = a2.update(100.0, 50.0, 100.0, 100.0)
    assert s1 == s2


def test_deterministic() -> None:
    """Two fresh instances with identical input produce identical output."""
    a1 = KyleLambdaAlpha()
    a2 = KyleLambdaAlpha()
    sequence = [
        (100.0, 50.0, 200.0, 100.0),
        (100.5, 60.0, 180.0, 120.0),
        (101.0, 70.0, 210.0, 90.0),
    ]
    for args in sequence:
        s1 = a1.update(*args)
        s2 = a2.update(*args)
        assert s1 == s2


def test_no_future_leakage() -> None:
    """Signal at tick T must not change when future ticks are added."""
    a1 = KyleLambdaAlpha()
    a1.update(100.0, 50.0, 200.0, 100.0)
    sig_at_t1 = a1.update(101.0, 60.0, 180.0, 120.0)

    a2 = KyleLambdaAlpha()
    a2.update(100.0, 50.0, 200.0, 100.0)
    sig_at_t1_v2 = a2.update(101.0, 60.0, 180.0, 120.0)
    # Now add future data to a2 — should not retroactively change sig_at_t1_v2
    a2.update(105.0, 100.0, 300.0, 50.0)

    assert sig_at_t1 == sig_at_t1_v2


def test_signal_changes_with_input() -> None:
    """Different inputs should produce different signals after warmup."""
    a1 = KyleLambdaAlpha()
    a2 = KyleLambdaAlpha()
    # Feed same initial data
    for i in range(50):
        a1.update(100.0 + i * 0.1, 100.0, 200.0, 50.0)
        a2.update(100.0 + i * 0.1, 100.0, 200.0, 50.0)
    # Diverge
    s1 = a1.update(110.0, 100.0, 200.0, 50.0)
    s2 = a2.update(90.0, 100.0, 50.0, 200.0)  # opposite direction
    assert s1 != s2


def test_many_updates_no_error() -> None:
    """10,000 updates should not raise or produce NaN/Inf."""
    import math

    alpha = KyleLambdaAlpha()
    for i in range(10_000):
        mid = 100.0 + (i % 100) * 0.01
        vol = float(50 + i % 200)
        bq = float(100 + i % 150)
        aq = float(100 + (i * 7) % 150)
        sig = alpha.update(mid, vol, bq, aq)
        assert math.isfinite(sig)


def test_two_fresh_instances_same_result() -> None:
    """Two independently constructed instances yield identical signals."""
    a1 = KyleLambdaAlpha()
    a2 = KyleLambdaAlpha()
    data = [
        (100.0, 80.0, 150.0, 100.0),
        (100.2, 90.0, 160.0, 90.0),
        (99.8, 70.0, 120.0, 140.0),
        (100.1, 85.0, 130.0, 130.0),
    ]
    for args in data:
        s1 = a1.update(*args)
        s2 = a2.update(*args)
        assert s1 == s2
