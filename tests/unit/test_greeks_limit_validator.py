"""Tests for GreeksLimitValidator."""
from unittest.mock import MagicMock


def _make_mock_provider(net_delta=0.0, net_gamma=0.0):
    from hft_platform.options.greeks import AggregatedGreeks

    provider = MagicMock()
    agg = AggregatedGreeks(
        net_delta=net_delta,
        net_gamma=net_gamma,
        net_theta_ntd=0.0,
        net_vega_ntd=0.0,
        positions=(),
    )
    provider.simulated_greeks_after.return_value = agg
    provider.current_portfolio_greeks.return_value = agg
    return provider


def test_validator_passes_when_disabled():
    from hft_platform.risk.greeks_limit_validator import GreeksLimitValidator

    v = GreeksLimitValidator({"greeks_limits": {"enabled": False, "net_delta_lots": 10}}, None)
    ok, reason = v.check(MagicMock())
    assert ok is True and reason == ""


def test_validator_passes_when_no_provider():
    from hft_platform.risk.greeks_limit_validator import GreeksLimitValidator

    v = GreeksLimitValidator({"greeks_limits": {"enabled": True, "net_delta_lots": 10}}, None)
    ok, reason = v.check(MagicMock())
    assert ok is True


def test_validator_passes_within_limits():
    from hft_platform.risk.greeks_limit_validator import GreeksLimitValidator

    provider = _make_mock_provider(net_delta=5.0, net_gamma=2.0)
    v = GreeksLimitValidator(
        {"greeks_limits": {"enabled": True, "net_delta_lots": 50, "net_gamma_lots": 20}},
        provider,
    )
    ok, reason = v.check(MagicMock())
    assert ok is True


def test_validator_rejects_delta_breach():
    from hft_platform.risk.greeks_limit_validator import GreeksLimitValidator

    provider = _make_mock_provider(net_delta=55.0)
    v = GreeksLimitValidator({"greeks_limits": {"enabled": True, "net_delta_lots": 50}}, provider)
    ok, reason = v.check(MagicMock())
    assert ok is False and reason == "GREEKS_DELTA_LIMIT"


def test_validator_rejects_negative_delta_breach():
    from hft_platform.risk.greeks_limit_validator import GreeksLimitValidator

    provider = _make_mock_provider(net_delta=-55.0)
    v = GreeksLimitValidator({"greeks_limits": {"enabled": True, "net_delta_lots": 50}}, provider)
    ok, reason = v.check(MagicMock())
    assert ok is False and reason == "GREEKS_DELTA_LIMIT"


def test_validator_rejects_gamma_breach():
    from hft_platform.risk.greeks_limit_validator import GreeksLimitValidator

    provider = _make_mock_provider(net_delta=5.0, net_gamma=25.0)
    v = GreeksLimitValidator(
        {"greeks_limits": {"enabled": True, "net_delta_lots": 50, "net_gamma_lots": 20}},
        provider,
    )
    ok, reason = v.check(MagicMock())
    assert ok is False and reason == "GREEKS_GAMMA_LIMIT"
