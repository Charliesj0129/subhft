# tests/unit/test_tca_impact.py
"""Tests for TCA SqrtImpactModel."""
from __future__ import annotations

import pytest

from hft_platform.tca.impact import SqrtImpactModel


class TestSqrtImpactModel:
    def setup_method(self) -> None:
        self.model = SqrtImpactModel(sigma_daily=0.015, adv=5000)

    def test_zero_volume_returns_zero_impact(self) -> None:
        assert self.model.estimate_impact_bps(volume=0) == 0.0

    def test_positive_volume_returns_positive_impact(self) -> None:
        impact = self.model.estimate_impact_bps(volume=100)
        assert impact > 0

    def test_impact_increases_with_volume(self) -> None:
        small = self.model.estimate_impact_bps(volume=50)
        large = self.model.estimate_impact_bps(volume=200)
        assert large > small

    def test_impact_sublinear_in_volume(self) -> None:
        single = self.model.estimate_impact_bps(volume=100)
        double = self.model.estimate_impact_bps(volume=200)
        assert double < 2.0 * single

    def test_zero_adv_returns_zero(self) -> None:
        model = SqrtImpactModel(sigma_daily=0.015, adv=0)
        assert model.estimate_impact_bps(volume=100) == 0.0
