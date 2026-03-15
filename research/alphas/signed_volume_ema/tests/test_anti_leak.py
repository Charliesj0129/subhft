"""Gate B anti-leak / lookahead-bias tests for SignedVolumeEmaAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.signed_volume_ema.impl import SignedVolumeEmaAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (all fields 0) must return a numeric value."""
    alpha = SignedVolumeEmaAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    """Same inputs produce identical signals -- no hidden randomness."""
    a1 = SignedVolumeEmaAlpha()
    a2 = SignedVolumeEmaAlpha()
    inputs = [(100.0, 500.0, 100.0), (50.0, 100.0, 500.0), (200.0, 300.0, 300.0)]
    for args in inputs:
        s1 = a1.update(*args)
        s2 = a2.update(*args)
        assert s1 == s2


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = SignedVolumeEmaAlpha()
    a2 = SignedVolumeEmaAlpha()
    a1.update(200.0, 800.0, 200.0)
    a1.reset()
    s1 = a1.update(50.0, 300.0, 300.0)
    s2 = a2.update(50.0, 300.0, 300.0)
    assert s1 == s2
