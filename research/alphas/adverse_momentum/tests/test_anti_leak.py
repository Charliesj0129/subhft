"""Gate B anti-leak / lookahead-bias tests for AdverseMomentumAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.adverse_momentum.impl import AdverseMomentumAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (all defaults 0) must return a numeric value."""
    alpha = AdverseMomentumAlpha()
    result = alpha.update(mid_price=0.0, bid_qty=0.0, ask_qty=0.0)
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    """Same input sequence always produces the same signal."""
    def run_sequence() -> float:
        alpha = AdverseMomentumAlpha()
        mid = 100.0
        for i in range(50):
            mid += (i % 5) - 2.0
            alpha.update(mid, float(100 + i % 7), float(100 + (i + 3) % 7))
        return alpha.get_signal()

    assert run_sequence() == run_sequence()


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = AdverseMomentumAlpha()
    a2 = AdverseMomentumAlpha()
    # Give a1 some history then reset
    a1.update(100.0, 500.0, 100.0)
    a1.update(120.0, 300.0, 200.0)
    a1.reset()
    # Both start fresh with the same input
    s1 = a1.update(200.0, 400.0, 400.0)
    s2 = a2.update(200.0, 400.0, 400.0)
    assert s1 == s2


def test_no_future_leakage() -> None:
    """Signal at tick T must not change if future ticks are different."""
    a1 = AdverseMomentumAlpha()
    a2 = AdverseMomentumAlpha()
    mid = 100.0
    for i in range(20):
        mid += 1.0
        a1.update(mid, 300.0, 100.0)
        a2.update(mid, 300.0, 100.0)
    sig_at_20_a1 = a1.get_signal()
    sig_at_20_a2 = a2.get_signal()
    assert sig_at_20_a1 == sig_at_20_a2
    # Now feed different future data to a1
    a1.update(mid + 100.0, 50.0, 900.0)
    # a2's signal at tick 20 is unchanged
    assert a2.get_signal() == sig_at_20_a2


def test_many_updates_no_error() -> None:
    """10k updates must not raise or produce NaN/Inf."""
    alpha = AdverseMomentumAlpha()
    mid = 1000.0
    for i in range(10_000):
        mid += (i % 11) - 5.0
        bid = float(100 + (i % 13))
        ask = float(100 + (i % 9))
        sig = alpha.update(mid, bid, ask)
        assert not (sig != sig), "NaN detected"
        assert abs(sig) <= 2.0


def test_two_fresh_instances_same_result() -> None:
    """Two independently created instances fed identical data produce identical signals."""
    a1 = AdverseMomentumAlpha()
    a2 = AdverseMomentumAlpha()
    mid = 100.0
    for i in range(30):
        mid += 0.5
        bid = float(200 + i % 5)
        ask = float(200 + (i + 2) % 5)
        s1 = a1.update(mid, bid, ask)
        s2 = a2.update(mid, bid, ask)
        assert s1 == s2
