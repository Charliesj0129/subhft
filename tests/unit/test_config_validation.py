"""Unit 6: Config validation tests."""

import pytest

from hft_platform.config.schema import ConfigValidationError, validate_config


def test_valid_config_passes():
    r = validate_config(
        {
            "mode": "sim",
            "symbols": ["2330"],
            "broker": "shioaji",
            "prometheus_port": 9090,
            "strategy": {"id": "mm", "module": "hft_platform.strategies.simple_mm", "class": "SimpleMarketMaker"},
        }
    )
    assert r.mode == "sim"


def test_invalid_mode_raises():
    with pytest.raises(ConfigValidationError, match="mode must be one of"):
        validate_config({"mode": "production", "symbols": ["2330"]})


def test_missing_symbols_raises():
    with pytest.raises(ConfigValidationError, match="symbols list must not be empty"):
        validate_config({"mode": "sim", "symbols": []})


def test_invalid_broker_raises():
    with pytest.raises(ConfigValidationError, match="broker must be one of"):
        validate_config({"mode": "sim", "symbols": ["2330"], "broker": "ib"})
