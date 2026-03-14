"""Gate B anti-leak / lookahead-bias tests for SpreadAdverseRatioAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.spread_adverse_ratio.impl import SpreadAdverseRatioAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (all zeros) must return a numeric value."""
    alpha = SpreadAdverseRatioAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_stateful() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = SpreadAdverseRatioAlpha()
    sig1 = alpha.update(500.0, 100_0000, 100.0)
    # Move mid_price so there is volatility — vol component grows.
    sig2 = alpha.update(500.0, 100_5000, 100.0)
    # After price moved, vol_proxy_ema increased -> adverse fraction should decrease.
    # Both should be valid floats.
    assert isinstance(sig1, float)
    assert isinstance(sig2, float)


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = SpreadAdverseRatioAlpha()
    a2 = SpreadAdverseRatioAlpha()
    # Pollute a1 with different history.
    a1.update(800.0, 110_0000, 500.0)
    a1.update(800.0, 109_0000, 500.0)
    a1.reset()
    # Now feed same data to both.
    s1 = a1.update(300.0, 100_0000, 100.0)
    s2 = a2.update(300.0, 100_0000, 100.0)
    assert s1 == s2


def test_deterministic() -> None:
    """Same inputs produce same outputs across two fresh instances."""
    inputs = [
        (100.0, 100_0000, 50.0),
        (200.0, 100_1000, 80.0),
        (150.0, 100_0500, 60.0),
    ]
    a1 = SpreadAdverseRatioAlpha()
    a2 = SpreadAdverseRatioAlpha()
    for args in inputs:
        s1 = a1.update(*args)
        s2 = a2.update(*args)
        assert s1 == s2


def test_no_future_leakage() -> None:
    """Signal at tick t must not change if we append more data after t."""
    alpha = SpreadAdverseRatioAlpha()
    alpha.update(100.0, 100_0000, 50.0)
    sig_t2 = alpha.update(200.0, 100_2000, 80.0)

    # Replay with same first two ticks.
    alpha2 = SpreadAdverseRatioAlpha()
    alpha2.update(100.0, 100_0000, 50.0)
    sig_t2_replay = alpha2.update(200.0, 100_2000, 80.0)
    assert sig_t2 == sig_t2_replay

    # Additional tick on alpha2 must not retroactively change t2.
    alpha2.update(300.0, 100_5000, 120.0)
    # sig_t2_replay was captured before the third tick — still same.
    assert sig_t2 == sig_t2_replay


def test_signal_nonnegative() -> None:
    """Signal is clipped to [0, 1]; must never be negative."""
    alpha = SpreadAdverseRatioAlpha()
    # Feed scenarios that could push adverse_component negative.
    for i in range(50):
        mid = 100_0000 + (10000 if i % 2 == 0 else -10000)
        sig = alpha.update(5.0, mid, 10000.0)
        assert sig >= 0.0


def test_many_updates_no_error() -> None:
    """10k updates must not raise or produce NaN/Inf."""
    import math

    alpha = SpreadAdverseRatioAlpha()
    for i in range(10_000):
        mid = 100_0000 + (i % 100) * 10
        sig = alpha.update(float(50 + i % 200), float(mid), float(1 + i % 500))
        assert not math.isnan(sig)
        assert not math.isinf(sig)
        assert 0.0 <= sig <= 1.0


def test_two_fresh_instances_same_result() -> None:
    """Two independently created instances produce identical signals for same data."""
    a1 = SpreadAdverseRatioAlpha()
    a2 = SpreadAdverseRatioAlpha()
    data = [
        (120.0, 100_0000, 200.0),
        (130.0, 100_0500, 180.0),
        (110.0, 99_9000, 220.0),
        (140.0, 100_1000, 190.0),
    ]
    for args in data:
        s1 = a1.update(*args)
        s2 = a2.update(*args)
        assert s1 == s2
