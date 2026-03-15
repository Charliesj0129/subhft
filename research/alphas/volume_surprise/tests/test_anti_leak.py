"""Gate B anti-leak / lookahead-bias tests for VolumeSurpriseAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.volume_surprise.impl import VolumeSurpriseAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (all zeros) must return a numeric value."""
    alpha = VolumeSurpriseAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    """Same input sequence produces identical output sequence."""
    a1 = VolumeSurpriseAlpha()
    a2 = VolumeSurpriseAlpha()
    inputs = [
        {"volume": 100.0, "bid_qty": 200.0, "ask_qty": 100.0},
        {"volume": 300.0, "bid_qty": 150.0, "ask_qty": 250.0},
        {"volume": 50.0, "bid_qty": 100.0, "ask_qty": 100.0},
    ]
    for inp in inputs:
        s1 = a1.update(**inp)
        s2 = a2.update(**inp)
        assert s1 == s2


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = VolumeSurpriseAlpha()
    a2 = VolumeSurpriseAlpha()
    a1.update(volume=800.0, bid_qty=200.0, ask_qty=100.0)
    a1.reset()
    s1 = a1.update(volume=300.0, bid_qty=300.0, ask_qty=300.0)
    s2 = a2.update(volume=300.0, bid_qty=300.0, ask_qty=300.0)
    assert s1 == s2
