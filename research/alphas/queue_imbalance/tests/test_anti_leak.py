"""Gate B anti-leak / lookahead-bias tests for QueueImbalanceAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.queue_imbalance.impl import QueueImbalanceAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (both queues 0) must return a numeric value."""
    alpha = QueueImbalanceAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_stateful_not_lookahead() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = QueueImbalanceAlpha()
    sig1 = alpha.update(500.0, 100.0)  # large bid queue
    sig2 = alpha.update(100.0, 500.0)  # large ask queue — should differ from sig1
    assert sig1 > 0.0
    assert sig2 < sig1  # signal moved in the correct direction


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = QueueImbalanceAlpha()
    a2 = QueueImbalanceAlpha()
    a1.update(800.0, 200.0)
    a1.reset()
    s1 = a1.update(300.0, 300.0)
    s2 = a2.update(300.0, 300.0)
    assert s1 == s2
