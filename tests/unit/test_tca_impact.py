"""Tests for SqrtImpactModel."""
from __future__ import annotations

import math

from hft_platform.tca.impact import SqrtImpactModel


class TestSqrtImpactModel:
    def setup_method(self):
        self.model = SqrtImpactModel(eta=1.0)

    def test_returns_float(self):
        result = self.model.estimate(qty=100, volatility=10.0, avg_volume=1000.0)
        assert isinstance(result, float)

    def test_zero_avg_volume_returns_zero(self):
        result = self.model.estimate(qty=100, volatility=10.0, avg_volume=0.0)
        assert result == 0.0

    def test_negative_avg_volume_returns_zero(self):
        result = self.model.estimate(qty=100, volatility=10.0, avg_volume=-50.0)
        assert result == 0.0

    def test_sqrt_scaling_with_qty(self):
        # Impact should scale with sqrt(qty): 4x qty → 2x impact
        base = self.model.estimate(qty=100, volatility=10.0, avg_volume=1000.0)
        quadrupled = self.model.estimate(qty=400, volatility=10.0, avg_volume=1000.0)
        assert abs(quadrupled - 2 * base) < 1e-9

    def test_linear_scaling_with_volatility(self):
        # Impact should scale linearly with volatility
        low = self.model.estimate(qty=100, volatility=5.0, avg_volume=1000.0)
        high = self.model.estimate(qty=100, volatility=15.0, avg_volume=1000.0)
        assert abs(high - 3 * low) < 1e-9

    def test_formula_correctness(self):
        # Impact = volatility * sqrt(qty / avg_volume) * eta
        qty = 200
        vol = 8.0
        avg_vol = 500.0
        eta = 1.0
        expected = vol * math.sqrt(qty / avg_vol) * eta
        result = self.model.estimate(qty=qty, volatility=vol, avg_volume=avg_vol)
        assert abs(result - expected) < 1e-12

    def test_eta_calibration_scales_result(self):
        # eta=2.0 should give exactly twice the impact of eta=1.0
        model_1 = SqrtImpactModel(eta=1.0)
        model_2 = SqrtImpactModel(eta=2.0)
        r1 = model_1.estimate(qty=100, volatility=10.0, avg_volume=1000.0)
        r2 = model_2.estimate(qty=100, volatility=10.0, avg_volume=1000.0)
        assert abs(r2 - 2 * r1) < 1e-12

    def test_inverse_volume_scaling(self):
        # Doubling avg_volume reduces impact by 1/sqrt(2)
        base = self.model.estimate(qty=100, volatility=10.0, avg_volume=1000.0)
        double_vol = self.model.estimate(qty=100, volatility=10.0, avg_volume=2000.0)
        assert abs(double_vol - base / math.sqrt(2)) < 1e-9

    def test_unit_participation_rate_identity(self):
        # When qty == avg_volume, impact == volatility * eta
        eta = 1.5
        model = SqrtImpactModel(eta=eta)
        vol = 12.0
        result = model.estimate(qty=500, volatility=vol, avg_volume=500.0)
        assert abs(result - vol * eta) < 1e-12

    def test_small_qty_gives_small_impact(self):
        result = self.model.estimate(qty=1, volatility=10.0, avg_volume=10_000.0)
        assert result < 1.0  # 1/100 participation → impact < 1 bps

    def test_default_eta_is_one(self):
        model = SqrtImpactModel()
        manual = SqrtImpactModel(eta=1.0)
        r1 = model.estimate(qty=100, volatility=10.0, avg_volume=1000.0)
        r2 = manual.estimate(qty=100, volatility=10.0, avg_volume=1000.0)
        assert r1 == r2
