"""Gate B anti-leak / lookahead-bias tests for CumOfiRevertAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.cum_ofi_revert.impl import CumOfiRevertAlpha


def test_no_future_leak() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = CumOfiRevertAlpha()
    sig1 = alpha.update(500.0)
    sig2 = alpha.update(-500.0)
    # Signal should move in opposite direction after opposite input
    assert sig1 < 0.0  # positive cum → negative signal
    assert sig2 > sig1  # negative cum pushes signal upward


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = CumOfiRevertAlpha()
    a2 = CumOfiRevertAlpha()
    a1.update(800.0)
    a1.reset()
    s1 = a1.update(300.0)
    s2 = a2.update(300.0)
    assert s1 == s2


def test_no_global_state_leak() -> None:
    """Two independent instances must not share state."""
    a1 = CumOfiRevertAlpha()
    a2 = CumOfiRevertAlpha()
    a1.update(1000.0)
    s2 = a2.update(0.0)
    assert s2 == 0.0


def test_order_matters() -> None:
    """Different orderings of the same values produce different signals."""
    a1 = CumOfiRevertAlpha()
    a2 = CumOfiRevertAlpha()
    a1.update(100.0)
    s1 = a1.update(900.0)
    a2.update(900.0)
    s2 = a2.update(100.0)
    assert s1 != s2


def test_no_lookahead_incremental() -> None:
    """Signal at tick N must be deterministic given ticks 0..N only."""
    alpha = CumOfiRevertAlpha()
    signals = []
    values = [100.0, 200.0, 300.0, -100.0, -200.0]
    for v in values:
        signals.append(alpha.update(v))

    # Replay: signal at each step should match
    alpha2 = CumOfiRevertAlpha()
    for i, v in enumerate(values):
        s = alpha2.update(v)
        assert s == signals[i]
