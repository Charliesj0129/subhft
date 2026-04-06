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


def _make_mock_provider_full(
    net_delta: float = 0.0,
    net_gamma: float = 0.0,
    net_vega_ntd: float = 0.0,
    net_theta_ntd: float = 0.0,
) -> MagicMock:
    from hft_platform.options.greeks import AggregatedGreeks

    provider = MagicMock()
    agg = AggregatedGreeks(
        net_delta=net_delta,
        net_gamma=net_gamma,
        net_theta_ntd=net_theta_ntd,
        net_vega_ntd=net_vega_ntd,
        positions=(),
    )
    provider.simulated_greeks_after.return_value = agg
    provider.current_portfolio_greeks.return_value = agg
    return provider


def test_validator_rejects_vega_breach():
    """Exceeding net_vega_ntd limit returns GREEKS_VEGA_LIMIT (line 39)."""
    from hft_platform.risk.greeks_limit_validator import GreeksLimitValidator

    provider = _make_mock_provider_full(net_vega_ntd=150_000.0)
    v = GreeksLimitValidator(
        {
            "greeks_limits": {
                "enabled": True,
                "net_delta_lots": 999,
                "net_gamma_lots": 999,
                "net_vega_ntd": 100_000,
            }
        },
        provider,
    )
    ok, reason = v.check(MagicMock())
    assert ok is False
    assert reason == "GREEKS_VEGA_LIMIT"


def test_validator_rejects_negative_vega_breach():
    """Negative vega exceeding magnitude of net_vega_ntd limit returns GREEKS_VEGA_LIMIT."""
    from hft_platform.risk.greeks_limit_validator import GreeksLimitValidator

    provider = _make_mock_provider_full(net_vega_ntd=-150_000.0)
    v = GreeksLimitValidator(
        {"greeks_limits": {"enabled": True, "net_vega_ntd": 100_000}},
        provider,
    )
    ok, reason = v.check(MagicMock())
    assert ok is False
    assert reason == "GREEKS_VEGA_LIMIT"


def test_validator_rejects_theta_breach():
    """net_theta_ntd below the configured floor returns GREEKS_THETA_LIMIT (line 41)."""
    from hft_platform.risk.greeks_limit_validator import GreeksLimitValidator

    provider = _make_mock_provider_full(net_theta_ntd=-5_000.0)
    v = GreeksLimitValidator(
        {
            "greeks_limits": {
                "enabled": True,
                "net_delta_lots": 999,
                "net_gamma_lots": 999,
                "net_vega_ntd": 999_999_999,
                "net_theta_ntd": -3_000,
            }
        },
        provider,
    )
    ok, reason = v.check(MagicMock())
    assert ok is False
    assert reason == "GREEKS_THETA_LIMIT"


def test_validator_passes_theta_above_floor():
    """net_theta_ntd above the configured floor returns True (no limit breach)."""
    from hft_platform.risk.greeks_limit_validator import GreeksLimitValidator

    provider = _make_mock_provider_full(net_theta_ntd=-2_000.0)
    v = GreeksLimitValidator(
        {"greeks_limits": {"enabled": True, "net_theta_ntd": -3_000}},
        provider,
    )
    ok, reason = v.check(MagicMock())
    assert ok is True
    assert reason == ""


def test_validator_passes_when_provider_raises_exception():
    """If simulated_greeks_after raises, validator logs warning and returns (True, '') (lines 31-33)."""
    from hft_platform.risk.greeks_limit_validator import GreeksLimitValidator

    provider = MagicMock()
    provider.simulated_greeks_after.side_effect = RuntimeError("greeks engine unavailable")

    v = GreeksLimitValidator(
        {"greeks_limits": {"enabled": True, "net_delta_lots": 10}},
        provider,
    )
    ok, reason = v.check(MagicMock())
    assert ok is True
    assert reason == ""
