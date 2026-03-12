"""Gate B anti-leak / lookahead-bias tests for OfiAccelerationAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.ofi_acceleration.impl import OfiAccelerationAlpha


def test_no_future() -> None:
    """Signal at time t must not depend on data arriving after t."""
    alpha = OfiAccelerationAlpha()
    alpha.update(0.0)
    sig_t1 = alpha.update(10.0)
    # Now feed more data; sig_t1 should not have changed retroactively.
    alpha.update(50.0)
    alpha.update(100.0)
    # Re-create from scratch and replay only up to t1.
    a2 = OfiAccelerationAlpha()
    a2.update(0.0)
    sig_t1_replay = a2.update(10.0)
    assert sig_t1 == sig_t1_replay


def test_reset_leak() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = OfiAccelerationAlpha()
    a2 = OfiAccelerationAlpha()
    # Contaminate a1 with unrelated history.
    a1.update(999.0)
    a1.update(0.0)
    a1.reset()
    # Both should produce identical results on the same input.
    s1 = a1.update(30.0)
    s2 = a2.update(30.0)
    assert s1 == s2


def test_no_global() -> None:
    """Two independent instances must not share mutable state."""
    a1 = OfiAccelerationAlpha()
    a2 = OfiAccelerationAlpha()
    a1.update(0.0)
    a1.update(100.0)
    # a2 should be unaffected by a1.
    assert a2.get_signal() == 0.0


def test_order_matters() -> None:
    """Different input orderings must produce different signals (causal)."""
    a1 = OfiAccelerationAlpha()
    a2 = OfiAccelerationAlpha()
    # Sequence A: 0, 10, 20
    a1.update(0.0)
    a1.update(10.0)
    sig_a = a1.update(20.0)
    # Sequence B: 20, 10, 0
    a2.update(20.0)
    a2.update(10.0)
    sig_b = a2.update(0.0)
    assert sig_a != sig_b


def test_no_lookahead() -> None:
    """Signal must be computed from past data only, not future values."""
    alpha = OfiAccelerationAlpha()
    signals = []
    sequence = [0.0, 5.0, 15.0, 10.0, 25.0]
    for v in sequence:
        signals.append(alpha.update(v))
    # Verify first signal is always 0 (no prior data).
    assert signals[0] == 0.0
    # Each signal should only reflect data up to that point.
    # Re-run partial sequences and verify match.
    for end_idx in range(1, len(sequence)):
        a2 = OfiAccelerationAlpha()
        for v in sequence[: end_idx + 1]:
            s = a2.update(v)
        assert s == signals[end_idx]
