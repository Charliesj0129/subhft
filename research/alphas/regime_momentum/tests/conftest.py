"""Shared fixtures for regime_momentum tests."""
from __future__ import annotations

import pytest

from research.alphas.regime_momentum.impl import RegimeMomentumAlpha


@pytest.fixture
def alpha() -> RegimeMomentumAlpha:
    """Return a fresh, reset RegimeMomentumAlpha instance."""
    a = RegimeMomentumAlpha()
    a.reset()
    return a
