"""Unit tests for CriticalTrendReversion signal generator."""

from __future__ import annotations

import math

import numpy as np
import pytest

from research.alphas.critical_trend_reversion.impl import (
    CriticalTrendReversion,
    _HorizonState,
    _PHI_C_DEFAULT,
    _WARMUP_TICKS,
    HORIZONS_MIN,
)


class TestHorizonState:
    """Tests for _HorizonState EMA and phi computation."""

    def test_initial_phi_is_zero(self) -> None:
        hs = _HorizonState(horizon_min=32, phi_c=1.58, b_tilde=0.001, c_tilde=-0.0003)
        assert hs.phi == 0.0
        assert hs.tick_count == 0

    def test_positive_trend_gives_positive_phi(self) -> None:
        hs = _HorizonState(horizon_min=2, phi_c=2.0, b_tilde=-0.005, c_tilde=0.0015)
        # Feed consistently positive returns
        for _ in range(500):
            hs.update(0.001)
        assert hs.phi > 0.0

    def test_negative_trend_gives_negative_phi(self) -> None:
        hs = _HorizonState(horizon_min=2, phi_c=2.0, b_tilde=-0.005, c_tilde=0.0015)
        for _ in range(500):
            hs.update(-0.001)
        assert hs.phi < 0.0

    def test_zero_returns_give_zero_phi(self) -> None:
        hs = _HorizonState(horizon_min=2, phi_c=2.0, b_tilde=-0.005, c_tilde=0.0015)
        for _ in range(500):
            hs.update(0.0)
        assert hs.phi == 0.0

    def test_phi_is_clipped(self) -> None:
        hs = _HorizonState(horizon_min=2, phi_c=2.0, b_tilde=-0.005, c_tilde=0.0015)
        # Feed extreme positive returns
        for _ in range(1000):
            hs.update(0.1)
        assert hs.phi <= 3.0  # _SIGNAL_CLIP

    def test_expected_return_cubic(self) -> None:
        hs = _HorizonState(horizon_min=32, phi_c=1.58, b_tilde=0.001, c_tilde=-0.0003)
        hs.phi = 1.5
        er = hs.expected_return()
        expected = 0.001 * 1.5 + (-0.0003) * 1.5**3
        assert abs(er - expected) < 1e-10

    def test_is_above_critical(self) -> None:
        hs = _HorizonState(horizon_min=32, phi_c=1.58, b_tilde=0.001, c_tilde=-0.0003)
        hs.phi = 1.0
        assert not hs.is_above_critical()
        hs.phi = 2.0
        assert hs.is_above_critical()
        hs.phi = -2.0
        assert hs.is_above_critical()

    def test_reset_clears_state(self) -> None:
        hs = _HorizonState(horizon_min=2, phi_c=2.0, b_tilde=-0.005, c_tilde=0.0015)
        for _ in range(100):
            hs.update(0.001)
        assert hs.tick_count > 0
        hs.reset()
        assert hs.phi == 0.0
        assert hs.tick_count == 0
        assert hs.ema_ret == 0.0


class TestCriticalTrendReversion:
    """Tests for the main CTR signal generator."""

    def test_no_signal_during_warmup(self) -> None:
        ctr = CriticalTrendReversion(horizons_min=(2, 4))
        # Feed some ticks during warmup
        base = 200000  # mid_x2
        for i in range(_WARMUP_TICKS - 1):
            result = ctr.update(base + i)
            assert result["signal"] == 0.0
            assert result["direction"] == 0

    def test_no_signal_on_zero_price(self) -> None:
        ctr = CriticalTrendReversion(horizons_min=(2,))
        result = ctr.update(0)
        assert result["signal"] == 0.0

    def test_no_signal_on_random_walk(self) -> None:
        """Random walk should rarely trigger signals."""
        rng = np.random.default_rng(42)
        ctr = CriticalTrendReversion(
            horizons_min=(32, 64),
            actionable_horizons=(32, 64),
        )
        base = 200000
        price = base
        n_signals = 0
        n_ticks = 2000

        for _ in range(n_ticks):
            price += rng.integers(-2, 3)
            price = max(1, price)
            result = ctr.update(price)
            if result["direction"] != 0:
                n_signals += 1

        # Random walk should trigger very few signals
        signal_rate = n_signals / n_ticks
        assert signal_rate < 0.15  # Less than 15% of ticks

    def test_strong_uptrend_triggers_sell(self) -> None:
        """A strong uptrend should eventually trigger a contrarian sell signal."""
        ctr = CriticalTrendReversion(
            horizons_min=(2,),
            actionable_horizons=(2,),
            phi_c_overrides={2: 1.5},
        )
        price = 200000
        triggered = False

        for i in range(1000):
            price += 10  # Consistent up-move
            result = ctr.update(price)
            if result["direction"] == -1:  # Sell signal (contrarian)
                triggered = True
                break

        assert triggered, "Strong uptrend should trigger contrarian sell"

    def test_strong_downtrend_triggers_buy(self) -> None:
        """A strong downtrend should trigger a contrarian buy signal."""
        ctr = CriticalTrendReversion(
            horizons_min=(2,),
            actionable_horizons=(2,),
            phi_c_overrides={2: 1.5},
        )
        price = 200000
        triggered = False

        for i in range(1000):
            price -= 10  # Consistent down-move
            price = max(100, price)
            result = ctr.update(price)
            if result["direction"] == 1:  # Buy signal (contrarian)
                triggered = True
                break

        assert triggered, "Strong downtrend should trigger contrarian buy"

    def test_phi_dict_populated_after_warmup(self) -> None:
        ctr = CriticalTrendReversion(horizons_min=(2, 4, 8))
        price = 200000

        for i in range(_WARMUP_TICKS + 10):
            price += 1
            result = ctr.update(price)

        phi_dict = result["phi"]
        assert isinstance(phi_dict, dict)
        assert 2 in phi_dict
        assert 4 in phi_dict
        assert 8 in phi_dict

    def test_reset_clears_all_state(self) -> None:
        ctr = CriticalTrendReversion(horizons_min=(2,))
        price = 200000
        for i in range(500):
            ctr.update(price + i)

        assert ctr.warmed_up
        ctr.reset()
        assert not ctr.warmed_up

    def test_get_phi_returns_value(self) -> None:
        ctr = CriticalTrendReversion(horizons_min=(2, 4))
        price = 200000
        for i in range(500):
            ctr.update(price + i)

        phi2 = ctr.get_phi(2)
        assert isinstance(phi2, float)

    def test_get_phi_nonexistent_horizon(self) -> None:
        ctr = CriticalTrendReversion(horizons_min=(2,))
        assert ctr.get_phi(999) == 0.0

    def test_custom_phi_c_overrides(self) -> None:
        ctr = CriticalTrendReversion(
            horizons_min=(32,),
            phi_c_overrides={32: 1.0},  # Very low threshold
            actionable_horizons=(32,),
        )
        price = 200000
        triggered = False

        for i in range(500):
            price += 5
            result = ctr.update(price)
            if result["direction"] != 0:
                triggered = True
                break

        # Low threshold should trigger easily
        assert triggered

    def test_manifest_exists(self) -> None:
        ctr = CriticalTrendReversion()
        m = ctr.manifest
        assert m.alpha_id == "critical_trend_reversion"
        assert len(m.paper_refs) == 3
        assert "2006.07847" in m.paper_refs


class TestDefaultThresholds:
    """Tests for the default critical threshold values."""

    def test_phi_c_values_reasonable(self) -> None:
        """All critical thresholds should be between 1.0 and 5.0."""
        for h, phi_c in _PHI_C_DEFAULT.items():
            assert 1.0 <= phi_c <= 5.0, f"phi_c={phi_c} for horizon={h}min out of range"

    def test_all_horizons_have_defaults(self) -> None:
        for h in HORIZONS_MIN:
            assert h in _PHI_C_DEFAULT
