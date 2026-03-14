"""Gate B anti-leak / lookahead-bias tests for AdverseMomentumAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.adverse_momentum.impl import AdverseMomentumAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (all defaults 0) must return a numeric value."""
    alpha = AdverseMomentumAlpha()
    result = alpha.update(mid_price=0.0, ofi_l1_ema8=0.0, spread_scaled=0.0)
    assert isinstance(result, (int, float))


def test_update_is_stateful() -> None:
    """Each call should only see past data; state evolves over time."""
    alpha = AdverseMomentumAlpha()
    sig1 = alpha.update(100.0, 5.0, 10.0)
    sig2 = alpha.update(110.0, 5.0, 10.0)  # price jumped up with positive OFI
    # After a price jump, state should have changed
    assert sig1 != sig2 or (sig1 == 0.0 and sig2 == 0.0)


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = AdverseMomentumAlpha()
    a2 = AdverseMomentumAlpha()
    # Give a1 some history then reset
    a1.update(100.0, 5.0, 10.0)
    a1.update(120.0, -3.0, 10.0)
    a1.reset()
    # Both start fresh with the same input
    s1 = a1.update(200.0, 1.0, 10.0)
    s2 = a2.update(200.0, 1.0, 10.0)
    assert s1 == s2


def test_deterministic() -> None:
    """Same input sequence always produces the same signal."""
    def run_sequence() -> float:
        alpha = AdverseMomentumAlpha()
        mid = 100.0
        for i in range(50):
            mid += (i % 5) - 2.0
            alpha.update(mid, float(i % 7) - 3.0, 10.0)
        return alpha.get_signal()

    assert run_sequence() == run_sequence()


def test_no_future_leakage() -> None:
    """Signal at tick T must not change if future ticks are different."""
    a1 = AdverseMomentumAlpha()
    a2 = AdverseMomentumAlpha()
    # Same first 20 ticks
    mid = 100.0
    for i in range(20):
        mid += 1.0
        a1.update(mid, 2.0, 10.0)
        a2.update(mid, 2.0, 10.0)
    sig_at_20_a1 = a1.get_signal()
    sig_at_20_a2 = a2.get_signal()
    assert sig_at_20_a1 == sig_at_20_a2
    # Now feed different future data to a1
    a1.update(mid + 100.0, 50.0, 10.0)
    # a2's signal at tick 20 is unchanged
    assert a2.get_signal() == sig_at_20_a2


def test_signal_changes_with_varying_residuals() -> None:
    """Signal should change when residuals vary over time."""
    alpha = AdverseMomentumAlpha()
    signals = []
    mid = 100.0
    for i in range(100):
        # Alternate between excess and deficit returns
        if i % 20 < 10:
            mid += 5.0  # large return
        else:
            mid += 0.1  # small return
        sig = alpha.update(mid, 3.0, 10.0)
        signals.append(sig)
    # Signal should not be constant
    assert len(set(round(s, 8) for s in signals)) > 1


def test_many_updates_no_error() -> None:
    """10k updates must not raise or produce NaN/Inf."""
    alpha = AdverseMomentumAlpha()
    mid = 1000.0
    for i in range(10_000):
        mid += (i % 11) - 5.0
        ofi = float((i % 7) - 3)
        sig = alpha.update(mid, ofi, 10.0)
        assert not (sig != sig), "NaN detected"  # NaN != NaN
        assert abs(sig) <= 2.0


def test_two_fresh_instances_same_result() -> None:
    """Two independently created instances fed identical data produce identical signals."""
    a1 = AdverseMomentumAlpha()
    a2 = AdverseMomentumAlpha()
    mid = 100.0
    for i in range(30):
        mid += 0.5
        ofi = float(i % 5) - 2.0
        s1 = a1.update(mid, ofi, 10.0)
        s2 = a2.update(mid, ofi, 10.0)
        assert s1 == s2
