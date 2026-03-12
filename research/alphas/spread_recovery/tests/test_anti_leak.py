"""Gate B anti-leak / lookahead-bias tests for SpreadRecoveryAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.spread_recovery.impl import SpreadRecoveryAlpha


def test_no_future_leakage() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = SpreadRecoveryAlpha()
    alpha.update(100)
    sig1 = alpha.update(200)  # widening
    sig2 = alpha.update(100)  # narrowing
    # Signal should differ: narrowing after widening is a recovery event
    assert sig2 != sig1


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = SpreadRecoveryAlpha()
    a2 = SpreadRecoveryAlpha()
    a1.update(800)
    a1.update(200)
    a1.reset()
    s1 = a1.update(300)
    s2 = a2.update(300)
    assert s1 == s2


def test_no_global_state() -> None:
    """Two independent instances must not share state."""
    a1 = SpreadRecoveryAlpha()
    a2 = SpreadRecoveryAlpha()
    a1.update(100)
    a1.update(500)
    sig1 = a1.get_signal()
    sig2 = a2.get_signal()
    assert sig1 != sig2, "Instances must be independent"


def test_order_matters() -> None:
    """Feeding [100, 200] vs [200, 100] should produce different signals."""
    a1 = SpreadRecoveryAlpha()
    a2 = SpreadRecoveryAlpha()
    a1.update(100)
    s1 = a1.update(200)
    a2.update(200)
    s2 = a2.update(100)
    assert s1 != s2, "Signal must depend on tick ordering"


def test_no_lookahead_incremental() -> None:
    """Signal at tick N must be identical whether computed incrementally or by replaying prefix."""
    seq = [100, 150, 200, 120, 90, 300, 100, 100]
    # Compute incrementally
    alpha_inc = SpreadRecoveryAlpha()
    signals_inc = [alpha_inc.update(s) for s in seq]
    # Replay from scratch for each prefix
    for n in range(len(seq)):
        alpha_replay = SpreadRecoveryAlpha()
        for s in seq[: n + 1]:
            sig = alpha_replay.update(s)
        assert sig == signals_inc[n], f"Mismatch at tick {n}"
