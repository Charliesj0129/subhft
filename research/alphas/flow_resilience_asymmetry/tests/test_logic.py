"""Minimal Gate B test for flow_resilience_asymmetry."""
from __future__ import annotations

from research.alphas.flow_resilience_asymmetry.impl import ALPHA_CLASS


def test_instantiate_and_update():
    alpha = ALPHA_CLASS()
    result = alpha.update(price=100, volume=10)
    assert isinstance(result, float)


def test_reset():
    alpha = ALPHA_CLASS()
    alpha.update(price=100, volume=10)
    alpha.reset()
    assert alpha.value() == 0.0
