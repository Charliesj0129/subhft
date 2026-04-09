"""Tests for OptionsLiveAdapter (float→int boundary)."""

from unittest.mock import MagicMock

from hft_platform.contracts.strategy import Side as StrategySide


def _make_adapter(net_delta=5.0, net_gamma=2.0):
    from hft_platform.options.greeks import GreeksResult, PositionGreeks
    from hft_platform.options.live_adapter import OptionsLiveAdapter
    from hft_platform.options.surface import VolSurface

    positions = [
        PositionGreeks(
            symbol="TXO20000D6",
            qty=10,
            greeks=GreeksResult(delta=net_delta / 10, gamma=net_gamma / 10, theta=-50.0, vega=100.0, rho=-0.5),
        )
    ]
    return OptionsLiveAdapter(positions=positions, surface=VolSurface(), multiplier=50.0)


def test_current_portfolio_greeks():
    adapter = _make_adapter(net_delta=12.3)
    agg = adapter.current_portfolio_greeks()
    assert abs(agg.net_delta - 12.3) < 0.01


def test_compute_hedge_lots_rounds():
    adapter = _make_adapter(net_delta=12.7)
    lots = adapter.compute_hedge_lots()
    assert isinstance(lots, int)
    assert lots == 13


def test_compute_hedge_lots_zero_below_threshold():
    adapter = _make_adapter(net_delta=0.3)
    lots = adapter.compute_hedge_lots(threshold=1)
    assert lots == 0


def test_check_limits_within():
    adapter = _make_adapter(net_delta=5.0, net_gamma=2.0)
    ok, reason = adapter.check_limits({"net_delta_lots": 50, "net_gamma_lots": 20})
    assert ok is True and reason == ""


def test_check_limits_breach():
    adapter = _make_adapter(net_delta=55.0)
    ok, reason = adapter.check_limits({"net_delta_lots": 50})
    assert ok is False and "delta" in reason.lower()


def test_simulated_greeks_after():
    adapter = _make_adapter(net_delta=5.0)
    intent = MagicMock()
    agg = adapter.simulated_greeks_after(intent)
    assert hasattr(agg, "net_delta")


def test_simulated_greeks_after_adjusts_delta():
    """simulated_greeks_after should reflect the intent's position change (IntEnum BUY)."""
    adapter = _make_adapter(net_delta=5.0)
    intent = MagicMock(symbol="TXO20000D6", qty=5, side=StrategySide.BUY)
    agg = adapter.simulated_greeks_after(intent)
    # Current: 10 contracts * 0.5 delta = 5.0. After adding 5 more: 15 * 0.5 = 7.5
    assert abs(agg.net_delta - 7.5) < 0.01


def test_simulated_greeks_after_sell_reduces_delta():
    """Selling contracts should reduce net delta (IntEnum SELL)."""
    adapter = _make_adapter(net_delta=5.0)
    intent = MagicMock(symbol="TXO20000D6", qty=5, side=StrategySide.SELL)
    agg = adapter.simulated_greeks_after(intent)
    # Current: 10 * 0.5 = 5.0. After selling 5: 5 * 0.5 = 2.5
    assert abs(agg.net_delta - 2.5) < 0.01


def test_simulated_greeks_after_buy_string_fallback():
    """String 'BUY' side fallback still works for non-IntEnum callers."""
    adapter = _make_adapter(net_delta=5.0)
    intent = MagicMock(symbol="TXO20000D6", qty=5, side="BUY")
    agg = adapter.simulated_greeks_after(intent)
    assert abs(agg.net_delta - 7.5) < 0.01


def test_simulated_greeks_after_sell_string_fallback():
    """String 'SELL' side fallback still works for non-IntEnum callers."""
    adapter = _make_adapter(net_delta=5.0)
    intent = MagicMock(symbol="TXO20000D6", qty=5, side="SELL")
    agg = adapter.simulated_greeks_after(intent)
    assert abs(agg.net_delta - 2.5) < 0.01


def test_simulated_greeks_after_unknown_symbol_returns_current():
    """Unknown symbol should return current Greeks unchanged."""
    adapter = _make_adapter(net_delta=5.0)
    intent = MagicMock(symbol="UNKNOWN_OPT", qty=10, side=StrategySide.BUY)
    agg = adapter.simulated_greeks_after(intent)
    assert abs(agg.net_delta - 5.0) < 0.01


def test_stress_limit_configurable():
    """run_stress should accept max_loss_ntd parameter."""
    import inspect

    adapter = _make_adapter()
    sig = inspect.signature(adapter.run_stress)
    assert "max_loss_ntd" in sig.parameters
