"""Minimal Gate B test for ofi_response_ratio."""
from __future__ import annotations

from research.alphas.ofi_response_ratio.impl import ALPHA_CLASS


def test_instantiate_and_update():
    alpha = ALPHA_CLASS()
    result = alpha.update(price=100, volume=10)
    assert isinstance(result, float)


def test_reset():
    alpha = ALPHA_CLASS()
    alpha.update(price=100, volume=10)
    alpha.reset()
    assert alpha.value() == 0.0
