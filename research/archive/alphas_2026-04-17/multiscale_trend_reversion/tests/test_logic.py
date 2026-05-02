"""Unit tests for MultiscaleTrendReversion signal generator and IC analysis."""

from __future__ import annotations

import math

import numpy as np
import pytest

from research.alphas.multiscale_trend_reversion.impl import (
    MultiscaleTrendReversion,
    _HorizonState,
    _PHI_C,
    _WARMUP_TICKS,
    HORIZONS_MIN,
    compute_forward_returns,
    compute_phi_series,
    fit_cubic,
    rank_ic,
)


# ---------------------------------------------------------------------------
# _HorizonState tests
# ---------------------------------------------------------------------------


class TestHorizonState:
    def test_initial_state_is_zero(self) -> None:
        hs = _HorizonState(horizon_min=32, tick_rate=1.8)
        assert hs.phi == 0.0
        assert hs.tick_count == 0
        assert hs.ema_ret == 0.0

    def test_positive_trend_gives_positive_phi(self) -> None:
        hs = _HorizonState(horizon_min=2, tick_rate=1.8,
                           phi_c=2.0, b_tilde=-0.005, c_tilde=0.0015)
        for _ in range(500):
            hs.update(0.001)
        assert hs.phi > 0.0

    def test_negative_trend_gives_negative_phi(self) -> None:
        hs = _HorizonState(horizon_min=2, tick_rate=1.8,
                           phi_c=2.0, b_tilde=-0.005, c_tilde=0.0015)
        for _ in range(500):
            hs.update(-0.001)
        assert hs.phi < 0.0

    def test_zero_returns_give_zero_phi(self) -> None:
        hs = _HorizonState(horizon_min=2, tick_rate=1.8)
        for _ in range(500):
            hs.update(0.0)
        assert hs.phi == 0.0

    def test_phi_is_clipped(self) -> None:
        hs = _HorizonState(horizon_min=2, tick_rate=1.8)
        for _ in range(1000):
            hs.update(0.1)
        assert abs(hs.phi) <= 5.0

    def test_expected_return_cubic(self) -> None:
        hs = _HorizonState(horizon_min=32, tick_rate=1.8,
                           b_tilde=0.001, c_tilde=-0.0003)
        hs.phi = 1.5
        er = hs.expected_return()
        expected = 0.001 * 1.5 + (-0.0003) * 1.5**3
        assert abs(er - expected) < 1e-10

    def test_is_above_critical(self) -> None:
        hs = _HorizonState(horizon_min=32, tick_rate=1.8, phi_c=1.58)
        hs.phi = 1.0
        assert not hs.is_above_critical()
        hs.phi = 2.0
        assert hs.is_above_critical()
        hs.phi = -2.0
        assert hs.is_above_critical()

    def test_reset(self) -> None:
        hs = _HorizonState(horizon_min=2, tick_rate=1.8)
        for _ in range(100):
            hs.update(0.001)
        assert hs.tick_count > 0
        hs.reset()
        assert hs.phi == 0.0
        assert hs.tick_count == 0


# ---------------------------------------------------------------------------
# MultiscaleTrendReversion tests
# ---------------------------------------------------------------------------


class TestMultiscaleTrendReversion:
    def test_no_signal_during_warmup(self) -> None:
        mstr = MultiscaleTrendReversion(horizons_min=(2, 4))
        base = 20000.0
        for i in range(_WARMUP_TICKS - 1):
            result = mstr.update(base + i * 0.1)
            assert result["signal"] == 0.0

    def test_no_signal_on_zero_price(self) -> None:
        mstr = MultiscaleTrendReversion(horizons_min=(2,))
        result = mstr.update(0.0)
        assert result["signal"] == 0.0

    def test_no_signal_on_random_walk(self) -> None:
        rng = np.random.default_rng(42)
        mstr = MultiscaleTrendReversion(
            horizons_min=(32, 64), actionable_horizons=(32, 64),
        )
        price = 20000.0
        n_signals = 0
        n_ticks = 2000
        for _ in range(n_ticks):
            price += rng.normal(0, 1)
            price = max(100, price)
            result = mstr.update(price)
            if result["direction"] != 0:
                n_signals += 1
        # With corrected phi_c (~1.0 vs ~1.8), more signals expected from random walk
        assert n_signals / n_ticks < 0.30

    def test_strong_uptrend_triggers_sell(self) -> None:
        mstr = MultiscaleTrendReversion(
            horizons_min=(2,), actionable_horizons=(2,),
        )
        price = 20000.0
        triggered = False
        for _ in range(1000):
            price += 1.0
            result = mstr.update(price)
            if result["direction"] == -1:
                triggered = True
                break
        assert triggered, "Strong uptrend should trigger contrarian sell"

    def test_strong_downtrend_triggers_buy(self) -> None:
        mstr = MultiscaleTrendReversion(
            horizons_min=(2,), actionable_horizons=(2,),
        )
        price = 20000.0
        triggered = False
        for _ in range(1000):
            price -= 1.0
            price = max(100, price)
            result = mstr.update(price)
            if result["direction"] == 1:
                triggered = True
                break
        assert triggered, "Strong downtrend should trigger contrarian buy"

    def test_phi_dict_populated_after_warmup(self) -> None:
        mstr = MultiscaleTrendReversion(horizons_min=(2, 4, 8))
        price = 20000.0
        for i in range(_WARMUP_TICKS + 10):
            price += 0.1
            result = mstr.update(price)
        phi_dict = result["phi"]
        assert isinstance(phi_dict, dict)
        assert set(phi_dict.keys()) == {2, 4, 8}

    def test_reset_clears_state(self) -> None:
        mstr = MultiscaleTrendReversion(horizons_min=(2,))
        price = 20000.0
        for i in range(500):
            mstr.update(price + i)
        assert mstr.warmed_up
        mstr.reset()
        assert not mstr.warmed_up

    def test_get_phi(self) -> None:
        mstr = MultiscaleTrendReversion(horizons_min=(2, 4))
        price = 20000.0
        for i in range(500):
            mstr.update(price + i)
        assert isinstance(mstr.get_phi(2), float)
        assert mstr.get_phi(999) == 0.0

    def test_manifest(self) -> None:
        mstr = MultiscaleTrendReversion()
        m = mstr.manifest
        assert m.alpha_id == "multiscale_trend_reversion"
        assert "2501.16772" in m.paper_refs


# ---------------------------------------------------------------------------
# Batch analysis function tests
# ---------------------------------------------------------------------------


class TestComputePhiSeries:
    def test_output_shape_matches_input(self) -> None:
        mid = np.linspace(20000, 20100, 1000)
        phi = compute_phi_series(mid, horizon_min=2, tick_rate=1.8)
        assert phi.shape == mid.shape

    def test_uptrend_gives_positive_phi(self) -> None:
        mid = np.linspace(20000, 21000, 2000)
        phi = compute_phi_series(mid, horizon_min=4, tick_rate=1.8)
        # After warmup, phi should be positive for an uptrend
        assert phi[500:].mean() > 0.0

    def test_constant_price_gives_zero_phi(self) -> None:
        mid = np.full(1000, 20000.0)
        phi = compute_phi_series(mid, horizon_min=2, tick_rate=1.8)
        assert np.all(phi == 0.0)

    def test_phi_is_clipped(self) -> None:
        mid = np.exp(np.linspace(0, 2, 2000)) * 20000  # extreme uptrend
        phi = compute_phi_series(mid, horizon_min=2, tick_rate=1.8)
        assert np.all(np.abs(phi) <= 5.0)


class TestComputeForwardReturns:
    def test_shape_matches_input(self) -> None:
        mid = np.linspace(20000, 20100, 1000)
        fwd = compute_forward_returns(mid, fwd_ticks=10)
        assert fwd.shape == mid.shape

    def test_last_elements_are_nan(self) -> None:
        mid = np.linspace(20000, 20100, 1000)
        fwd = compute_forward_returns(mid, fwd_ticks=10)
        assert np.all(np.isnan(fwd[-10:]))

    def test_uptrend_has_positive_forward_returns(self) -> None:
        mid = np.linspace(20000, 21000, 1000)
        fwd = compute_forward_returns(mid, fwd_ticks=10)
        valid = fwd[~np.isnan(fwd)]
        assert valid.mean() > 0


class TestRankIC:
    def test_perfect_positive_correlation(self) -> None:
        signal = np.arange(1000, dtype=np.float64)
        fwd = np.arange(1000, dtype=np.float64)
        ic = rank_ic(signal, fwd, warmup=0)
        assert ic > 0.99

    def test_perfect_negative_correlation(self) -> None:
        signal = np.arange(1000, dtype=np.float64)
        fwd = -np.arange(1000, dtype=np.float64)
        ic = rank_ic(signal, fwd, warmup=0)
        assert ic < -0.99

    def test_no_correlation_near_zero(self) -> None:
        rng = np.random.default_rng(42)
        signal = rng.standard_normal(5000)
        fwd = rng.standard_normal(5000)
        ic = rank_ic(signal, fwd, warmup=0)
        assert abs(ic) < 0.1

    def test_insufficient_data_returns_zero(self) -> None:
        signal = np.array([1.0, 2.0, 3.0])
        fwd = np.array([1.0, 2.0, 3.0])
        ic = rank_ic(signal, fwd, warmup=0)
        assert ic == 0.0


class TestFitCubic:
    def test_linear_data_has_zero_cubic(self) -> None:
        rng = np.random.default_rng(42)
        phi = rng.standard_normal(2000)
        fwd = 0.01 * phi + rng.standard_normal(2000) * 0.001
        result = fit_cubic(phi, fwd, warmup=0)
        # c should be close to zero for linear relationship
        assert abs(result["c"]) < abs(result["b"]) * 0.5

    def test_output_keys(self) -> None:
        phi = np.random.default_rng(42).standard_normal(2000)
        fwd = np.random.default_rng(43).standard_normal(2000)
        result = fit_cubic(phi, fwd, warmup=0)
        assert "a" in result
        assert "b" in result
        assert "c" in result
        assert "r_squared" in result
        assert "phi_c" in result
        assert "n" in result


class TestDefaultThresholds:
    def test_phi_c_values_reasonable(self) -> None:
        # With corrected formula phi_c = sqrt(-b/(3c)), values are ~0.9-1.1
        for h, phi_c in _PHI_C.items():
            assert 0.5 <= phi_c <= 5.0, f"phi_c={phi_c} for h={h} out of range"

    def test_all_horizons_have_defaults(self) -> None:
        for h in HORIZONS_MIN:
            assert h in _PHI_C
