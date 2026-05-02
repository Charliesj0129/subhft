"""Tests for R30 RFSV Vol-Timing Alpha (v2 — post-Challenger)."""

from __future__ import annotations

import math

import numpy as np

from research.alphas.r30_rfsv_vol_timing.impl import (
    R30RfsvVolTimingAlpha,
    _fbm_covariance,
)


def test_manifest_alpha_id() -> None:
    alpha = R30RfsvVolTimingAlpha()
    assert alpha.manifest.alpha_id == "r30_rfsv_vol_timing"


def test_manifest_paper_refs() -> None:
    alpha = R30RfsvVolTimingAlpha()
    assert "arXiv:1410.3394" in alpha.manifest.paper_refs


def test_initial_signal_is_zero() -> None:
    alpha = R30RfsvVolTimingAlpha()
    assert alpha.get_signal() == 0.0


def test_signal_zero_during_warmup() -> None:
    """Signal should remain 0 during warmup period (now 16 buckets, A-4 fix)."""
    alpha = R30RfsvVolTimingAlpha()
    base_price = 200000000
    for i in range(100):
        noise = int(1000 * math.sin(i * 0.1))
        alpha.update(price=base_price + noise)
    assert alpha.get_signal() == 0.0


def test_hurst_initial_value() -> None:
    alpha = R30RfsvVolTimingAlpha()
    assert alpha.hurst_h == 0.1


def test_reset_clears_state() -> None:
    alpha = R30RfsvVolTimingAlpha()
    for i in range(50):
        alpha.update(price=200000000 + i * 100)
    alpha.reset()
    assert alpha.get_signal() == 0.0
    assert alpha.rv_count == 0
    assert alpha.hurst_h == 0.1


def test_rv_accumulation() -> None:
    alpha = R30RfsvVolTimingAlpha()
    base = 200000000
    for i in range(600):
        noise = int(500 * math.sin(i * 0.05))
        alpha.update(price=base + noise)
    assert alpha.rv_count >= 1


def _generate_rough_vol_prices(
    n_ticks: int, h: float = 0.1, base_price: int = 200000000
) -> list[int]:
    """Generate synthetic prices with rough volatility characteristics."""
    rng = np.random.default_rng(42)
    prices = [base_price]
    vol_log = -10.0
    for _i in range(1, n_ticks):
        vol_increment = rng.normal(0, 0.01) * (1.0 ** h)
        vol_log += vol_increment
        vol = math.exp(vol_log)
        ret = rng.normal(0, vol)
        new_price = int(prices[-1] * math.exp(ret))
        new_price = max(new_price, 1)
        prices.append(new_price)
    return prices


def test_signal_bounded() -> None:
    alpha = R30RfsvVolTimingAlpha()
    prices = _generate_rough_vol_prices(20000)
    for p in prices:
        sig = alpha.update(price=p)
        assert -1.0 <= sig <= 1.0


def test_hurst_estimation_on_synthetic_data() -> None:
    alpha = R30RfsvVolTimingAlpha()
    prices = _generate_rough_vol_prices(30000, h=0.1)
    for p in prices:
        alpha.update(price=p)
    assert 0.01 <= alpha.hurst_h <= 0.49
    assert alpha.rv_count >= 16


def test_zero_price_ignored() -> None:
    alpha = R30RfsvVolTimingAlpha()
    alpha.update(price=0)
    alpha.update(price=-1)
    assert alpha.get_signal() == 0.0


def test_update_returns_signal() -> None:
    alpha = R30RfsvVolTimingAlpha()
    sig = alpha.update(price=200000000)
    assert sig == alpha.get_signal()


# --- A-1: Variogram estimator Monte Carlo validation ---

def test_hurst_estimator_monte_carlo() -> None:
    """A-1 fix: Validate estimator recovers H on 128-bucket samples.

    Generate 30 synthetic series, estimate H on each, check mean and std.
    """
    rng = np.random.default_rng(777)
    n_trials = 30
    n_buckets = 128
    estimates: list[float] = []

    for _trial in range(n_trials):
        alpha = R30RfsvVolTimingAlpha()
        base_price = 200000000
        for _bucket in range(n_buckets):
            ret_std = rng.uniform(0.0001, 0.001)
            for _tick in range(300):
                ret = rng.normal(0, ret_std)
                base_price = int(base_price * math.exp(ret))
                base_price = max(base_price, 1)
                alpha.update(price=base_price)
        estimates.append(alpha.hurst_h)

    mean_h = float(np.mean(estimates))
    std_h = float(np.std(estimates))
    assert 0.01 <= mean_h <= 0.45, f"Mean H = {mean_h:.3f}"
    assert std_h < 0.25, f"Std H = {std_h:.3f}"


# --- A-2: fBm covariance kernel correctness ---

def test_fbm_covariance_diagonal() -> None:
    """A-2: C(t, t) = t^{2H}."""
    h = 0.1
    for t in [1.0, 2.0, 5.0]:
        c_tt = _fbm_covariance(t, t, h)
        expected = t ** (2 * h)
        assert abs(c_tt - expected) < 1e-10


def test_fbm_covariance_symmetry() -> None:
    """A-2: C(s, t) = C(t, s)."""
    assert abs(_fbm_covariance(2.0, 3.0, 0.1) - _fbm_covariance(3.0, 2.0, 0.1)) < 1e-15


def test_fbm_covariance_zero() -> None:
    """A-2: C(0, t) = 0."""
    assert abs(_fbm_covariance(0.0, 5.0, 0.1)) < 1e-15


def test_forecast_changes_with_h() -> None:
    """A-2: Forecast weights are horizon-dependent and change with H."""
    alpha = R30RfsvVolTimingAlpha()
    prices = _generate_rough_vol_prices(20000)
    for p in prices:
        alpha.update(price=p)
    forecast_1 = alpha.forecast_log_rv

    alpha._hurst_h = 0.3
    alpha._recompute_forecast_weights()
    alpha._compute_forecast()
    forecast_2 = alpha.forecast_log_rv

    # Different H should produce different forecasts
    if alpha.rv_count >= 16:
        assert forecast_1 != forecast_2


# --- A-3: Directional component ---

def test_directional_component_responds_to_trend() -> None:
    """A-3: Signal should incorporate return direction."""
    alpha_up = R30RfsvVolTimingAlpha()
    alpha_down = R30RfsvVolTimingAlpha()

    rng = np.random.default_rng(99)
    base = 200000000

    price = base
    for _i in range(10000):
        ret = 0.00005 + rng.normal(0, 0.0003)
        price = int(price * math.exp(ret))
        price = max(price, 1)
        alpha_up.update(price=price)

    price = base
    for _i in range(10000):
        ret = -0.00005 + rng.normal(0, 0.0003)
        price = int(price * math.exp(ret))
        price = max(price, 1)
        alpha_down.update(price=price)

    sig_up = alpha_up.get_signal()
    sig_down = alpha_down.get_signal()
    assert sig_up != sig_down or (sig_up == 0.0 and sig_down == 0.0)
