"""Gate B anti-leak / lookahead-bias tests for QuoteIntensityAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.quote_intensity.impl import QuoteIntensityAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (both queues 0) must return a numeric value."""
    alpha = QuoteIntensityAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    """Two fresh instances fed the same sequence produce identical signals."""
    a1 = QuoteIntensityAlpha()
    a2 = QuoteIntensityAlpha()
    sequence = [(100.0, 200.0), (150.0, 180.0), (200.0, 50.0), (120.0, 120.0)]
    for bid, ask in sequence:
        s1 = a1.update(bid, ask)
        s2 = a2.update(bid, ask)
        assert s1 == s2


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = QuoteIntensityAlpha()
    a2 = QuoteIntensityAlpha()
    a1.update(800.0, 200.0)
    a1.update(400.0, 600.0)
    a1.reset()
    s1 = a1.update(300.0, 300.0)
    s2 = a2.update(300.0, 300.0)
    assert s1 == s2
