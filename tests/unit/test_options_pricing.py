"""Unit tests for Black-76 pricing and IV solver.

Imports are lazy (inside each test function) per task spec.
"""

import math

import pytest


def test_black76_call_atm():
    from hft_platform.options.pricing import black76_price

    F, K, T, sigma, r = 100.0, 100.0, 1.0, 0.20, 0.05
    price = black76_price(F, K, T, sigma, r, "C")
    # ATM call must be positive
    assert price > 0
    # Brenner-Subrahmanyam closed-form approx: ~F * sigma * sqrt(T/(2*pi)) * exp(-rT)
    approx = F * sigma * math.sqrt(T / (2 * math.pi)) * math.exp(-r * T)
    assert abs(price - approx) / approx < 0.05


def test_black76_put_atm():
    from hft_platform.options.pricing import black76_price

    F, K, T, sigma, r = 100.0, 100.0, 1.0, 0.20, 0.05
    call = black76_price(F, K, T, sigma, r, "C")
    put = black76_price(F, K, T, sigma, r, "P")
    # F=K → C - P ≈ 0 (both discounted at same rate)
    assert abs(call - put) < 1e-8


def test_black76_deep_itm_call():
    from hft_platform.options.pricing import black76_price

    F, K, T, sigma, r = 150.0, 100.0, 1.0, 0.20, 0.05
    price = black76_price(F, K, T, sigma, r, "C")
    intrinsic = math.exp(-r * T) * (F - K)
    assert price >= intrinsic - 1e-10


def test_black76_deep_otm_call():
    from hft_platform.options.pricing import black76_price

    F, K, T, sigma, r = 50.0, 100.0, 1.0, 0.20, 0.05
    price = black76_price(F, K, T, sigma, r, "C")
    assert price >= 0
    assert price < 10


def test_black76_put_call_parity():
    from hft_platform.options.pricing import black76_price

    F, K, T, sigma, r = 105.0, 95.0, 0.5, 0.25, 0.03
    call = black76_price(F, K, T, sigma, r, "C")
    put = black76_price(F, K, T, sigma, r, "P")
    # C - P = e^{-rT}(F - K)
    expected = math.exp(-r * T) * (F - K)
    assert abs((call - put) - expected) < 1e-8


def test_black76_invalid_cp_raises():
    from hft_platform.options.pricing import black76_price

    with pytest.raises(ValueError):
        black76_price(100.0, 100.0, 1.0, 0.20, 0.05, "X")


def test_black76_zero_time_call():
    from hft_platform.options.pricing import black76_price

    # At expiry (T=0), call = max(F-K, 0)
    price_itm = black76_price(110.0, 100.0, 0.0, 0.20, 0.05, "C")
    assert abs(price_itm - 10.0) < 1e-10

    price_otm = black76_price(90.0, 100.0, 0.0, 0.20, 0.05, "C")
    assert abs(price_otm - 0.0) < 1e-10


def test_solve_iv_roundtrip():
    from hft_platform.options.pricing import black76_price, solve_iv

    F, K, T, r, sigma_true = 100.0, 100.0, 0.5, 0.03, 0.20
    market_price = black76_price(F, K, T, sigma_true, r, "C")
    iv = solve_iv(market_price, F, K, T, r, "C", tick_size=0.01)
    assert not math.isnan(iv)
    assert abs(iv - sigma_true) < 1e-6


def test_solve_iv_roundtrip_put():
    from hft_platform.options.pricing import black76_price, solve_iv

    F, K, T, r, sigma_true = 100.0, 105.0, 0.25, 0.02, 0.30
    market_price = black76_price(F, K, T, sigma_true, r, "P")
    iv = solve_iv(market_price, F, K, T, r, "P", tick_size=0.01)
    assert not math.isnan(iv)
    assert abs(iv - sigma_true) < 1e-6


def test_solve_iv_deep_otm_returns_nan():
    from hft_platform.options.pricing import solve_iv

    # Deep OTM: price well below 0.5 * tick_size
    iv = solve_iv(0.001, 100.0, 200.0, 0.5, 0.03, "C", tick_size=1.0)
    assert math.isnan(iv)


def test_solve_iv_negative_price_returns_nan():
    from hft_platform.options.pricing import solve_iv

    iv = solve_iv(-1.0, 100.0, 100.0, 0.5, 0.03, "C")
    assert math.isnan(iv)
