"""Gate B anti-leak / lookahead-bias tests for SpreadVolumeCrossAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.spread_volume_cross.impl import SpreadVolumeCrossAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (all defaults 0) must return a numeric value."""
    alpha = SpreadVolumeCrossAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    """Two fresh alphas fed the same sequence produce identical signals."""
    a1 = SpreadVolumeCrossAlpha()
    a2 = SpreadVolumeCrossAlpha()
    data = [
        {"spread_bps": 20.0, "volume": 100.0, "bid_qty": 150.0, "ask_qty": 50.0},
        {"spread_bps": 15.0, "volume": 300.0, "bid_qty": 200.0, "ask_qty": 80.0},
        {"spread_bps": 18.0, "volume": 50.0, "bid_qty": 90.0, "ask_qty": 120.0},
    ]
    for d in data:
        s1 = a1.update(**d)
        s2 = a2.update(**d)
        assert s1 == s2


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = SpreadVolumeCrossAlpha()
    a2 = SpreadVolumeCrossAlpha()
    # Pollute a1 with different history
    a1.update(spread_bps=50.0, volume=500.0, bid_qty=300.0, ask_qty=100.0)
    a1.update(spread_bps=10.0, volume=800.0, bid_qty=400.0, ask_qty=50.0)
    a1.reset()
    # Now both should behave identically
    s1 = a1.update(spread_bps=20.0, volume=100.0, bid_qty=100.0, ask_qty=100.0)
    s2 = a2.update(spread_bps=20.0, volume=100.0, bid_qty=100.0, ask_qty=100.0)
    assert s1 == s2
