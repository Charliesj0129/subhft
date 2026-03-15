"""Gate B anti-leak / lookahead-bias tests for OfiVolumeRatioAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.ofi_volume_ratio.impl import OfiVolumeRatioAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (all zeros) must return a numeric value."""
    alpha = OfiVolumeRatioAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    """Same inputs produce identical outputs across two fresh instances."""
    a1 = OfiVolumeRatioAlpha()
    a2 = OfiVolumeRatioAlpha()
    inputs = [(100.0, 50.0, 200.0), (80.0, 120.0, 300.0), (90.0, 90.0, 180.0)]
    for args in inputs:
        s1 = a1.update(*args)
        s2 = a2.update(*args)
        assert s1 == s2


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = OfiVolumeRatioAlpha()
    a2 = OfiVolumeRatioAlpha()
    a1.update(800.0, 200.0, 500.0)
    a1.reset()
    s1 = a1.update(300.0, 300.0, 600.0)
    s2 = a2.update(300.0, 300.0, 600.0)
    assert s1 == s2
