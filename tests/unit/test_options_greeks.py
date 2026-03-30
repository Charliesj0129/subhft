"""Tests for Black-76 Greeks and portfolio aggregation."""
import math
import pytest


def test_compute_greeks_call_delta_range():
    """Call delta in (0, 1)."""
    from hft_platform.options.greeks import compute_greeks
    g = compute_greeks(20000.0, 20000.0, 30/365, 0.20, 0.01, "C")
    assert 0 < g.delta < 1


def test_compute_greeks_put_delta_range():
    """Put delta in (-1, 0)."""
    from hft_platform.options.greeks import compute_greeks
    g = compute_greeks(20000.0, 20000.0, 30/365, 0.20, 0.01, "P")
    assert -1 < g.delta < 0


def test_compute_greeks_put_call_delta_parity():
    """Delta_C - Delta_P ≈ e^{-rT}."""
    from hft_platform.options.greeks import compute_greeks
    T, r = 30/365, 0.01
    gc = compute_greeks(20000.0, 19500.0, T, 0.20, r, "C")
    gp = compute_greeks(20000.0, 19500.0, T, 0.20, r, "P")
    assert abs((gc.delta - gp.delta) - math.exp(-r * T)) < 1e-6


def test_compute_greeks_gamma_positive():
    from hft_platform.options.greeks import compute_greeks
    gc = compute_greeks(20000.0, 20000.0, 30/365, 0.20, 0.01, "C")
    gp = compute_greeks(20000.0, 20000.0, 30/365, 0.20, 0.01, "P")
    assert gc.gamma > 0
    assert gp.gamma > 0


def test_compute_greeks_gamma_equal_call_put():
    from hft_platform.options.greeks import compute_greeks
    gc = compute_greeks(20000.0, 20000.0, 30/365, 0.20, 0.01, "C")
    gp = compute_greeks(20000.0, 20000.0, 30/365, 0.20, 0.01, "P")
    assert abs(gc.gamma - gp.gamma) < 1e-12


def test_compute_greeks_vega_positive():
    from hft_platform.options.greeks import compute_greeks
    g = compute_greeks(20000.0, 20000.0, 30/365, 0.20, 0.01, "C")
    assert g.vega > 0


def test_compute_greeks_theta_negative_for_long():
    from hft_platform.options.greeks import compute_greeks
    g = compute_greeks(20000.0, 20000.0, 30/365, 0.20, 0.01, "C")
    assert g.theta < 0


def test_portfolio_greeks_single_position():
    from hft_platform.options.greeks import PositionGreeks, compute_greeks, portfolio_greeks
    g = compute_greeks(20000.0, 20000.0, 30/365, 0.20, 0.01, "C")
    pos = [PositionGreeks(symbol="TXO20000C", qty=2, greeks=g)]
    agg = portfolio_greeks(pos, multiplier=50.0)
    assert abs(agg.net_delta - 2 * g.delta) < 1e-10


def test_portfolio_greeks_hedged_position():
    """Long call + short put: net delta ≈ e^{-rT}."""
    from hft_platform.options.greeks import PositionGreeks, compute_greeks, portfolio_greeks
    T, r = 30/365, 0.01
    gc = compute_greeks(20000.0, 20000.0, T, 0.20, r, "C")
    gp = compute_greeks(20000.0, 20000.0, T, 0.20, r, "P")
    pos = [
        PositionGreeks(symbol="TXO20000C", qty=1, greeks=gc),
        PositionGreeks(symbol="TXO20000P", qty=-1, greeks=gp),
    ]
    agg = portfolio_greeks(pos, multiplier=50.0)
    assert abs(agg.net_delta - math.exp(-r * T)) < 1e-6


def test_portfolio_greeks_empty():
    from hft_platform.options.greeks import portfolio_greeks
    agg = portfolio_greeks([], multiplier=50.0)
    assert agg.net_delta == 0.0
    assert agg.net_gamma == 0.0
    assert agg.net_theta_ntd == 0.0
    assert agg.net_vega_ntd == 0.0
