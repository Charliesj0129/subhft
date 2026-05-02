"""Tests for portfolio stress testing."""

from datetime import date


def test_stress_test_single_scenario():
    from hft_platform.options.greeks import GreeksResult, PositionGreeks
    from hft_platform.options.surface import VolSurface
    from hft_platform.risk.stress_test import ScenarioConfig, run_stress_test

    surface = VolSurface()
    d = date(2026, 4, 15)
    for strike, iv in [(19000, 0.25), (19500, 0.22), (20000, 0.20), (20500, 0.21), (21000, 0.24)]:
        surface.update(float(strike), d, iv)
    g = GreeksResult(delta=0.5, gamma=0.001, theta=-50.0, vega=100.0, rho=-0.5)
    positions = [PositionGreeks(symbol="TXO20000D6", qty=10, greeks=g)]
    scenarios = [ScenarioConfig(name="down_3pct", underlying_shift_pct=-3.0, vol_shift_abs=0.0)]
    results = run_stress_test(positions, surface, scenarios, 20000.0, 50.0)
    assert len(results) == 1
    assert results[0].name == "down_3pct"
    assert isinstance(results[0].pnl_ntd, float)
    assert results[0].pnl_ntd < 0  # long delta, price drops


def test_stress_test_vol_crush_short_straddle():
    from hft_platform.options.greeks import GreeksResult, PositionGreeks
    from hft_platform.options.surface import VolSurface
    from hft_platform.risk.stress_test import ScenarioConfig, run_stress_test

    surface = VolSurface()
    d = date(2026, 4, 15)
    for strike, iv in [(19000, 0.25), (19500, 0.22), (20000, 0.20), (20500, 0.21), (21000, 0.24)]:
        surface.update(float(strike), d, iv)
    gc = GreeksResult(delta=0.5, gamma=0.001, theta=-50.0, vega=100.0, rho=-0.5)
    gp = GreeksResult(delta=-0.5, gamma=0.001, theta=-50.0, vega=100.0, rho=0.5)
    positions = [
        PositionGreeks(symbol="TXO20000C", qty=-1, greeks=gc),
        PositionGreeks(symbol="TXO20000P", qty=-1, greeks=gp),
    ]
    scenarios = [ScenarioConfig(name="vol_crush", underlying_shift_pct=0.0, vol_shift_abs=-0.10)]
    results = run_stress_test(positions, surface, scenarios, 20000.0, 50.0)
    assert len(results) == 1
    assert results[0].pnl_ntd > 0  # short vega benefits


def test_stress_test_empty_positions():
    from hft_platform.options.surface import VolSurface
    from hft_platform.risk.stress_test import ScenarioConfig, run_stress_test

    results = run_stress_test([], VolSurface(), [ScenarioConfig("x", -1.0, 0.0)], 20000.0, 50.0)
    assert len(results) == 1
    assert results[0].pnl_ntd == 0.0


def test_stress_test_multiple_scenarios():
    from hft_platform.options.greeks import GreeksResult, PositionGreeks
    from hft_platform.options.surface import VolSurface
    from hft_platform.risk.stress_test import ScenarioConfig, run_stress_test

    surface = VolSurface()
    g = GreeksResult(delta=0.5, gamma=0.001, theta=-50.0, vega=100.0, rho=-0.5)
    positions = [PositionGreeks(symbol="TXO20000D6", qty=5, greeks=g)]
    scenarios = [
        ScenarioConfig("down_3", -3.0, 0.0),
        ScenarioConfig("down_3_vol_up", -3.0, 0.05),
        ScenarioConfig("vol_crush", 0.0, -0.10),
        ScenarioConfig("worst", -5.0, 0.10),
    ]
    results = run_stress_test(positions, surface, scenarios, 20000.0, 50.0)
    assert len(results) == 4
    assert [r.name for r in results] == ["down_3", "down_3_vol_up", "vol_crush", "worst"]
