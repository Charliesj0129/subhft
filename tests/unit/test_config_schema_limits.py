"""Tests for extended config schema validation (C-03)."""
import pytest
from hft_platform.config.schema import validate_config, ConfigValidationError


def test_valid_config_with_intraday_pnl():
    """Valid config with intraday_pnl passes validation."""
    cfg = {
        "mode": "sim",
        "symbols": ["2330"],
        "intraday_pnl": {
            "soft_limit_ntd": 500,
            "hard_limit_ntd": 1000,
        },
    }
    result = validate_config(cfg)
    assert result.mode == "sim"


def test_intraday_pnl_soft_exceeds_hard_rejected():
    """soft_limit_ntd > hard_limit_ntd should fail validation."""
    cfg = {
        "mode": "sim",
        "symbols": ["2330"],
        "intraday_pnl": {
            "soft_limit_ntd": 2000,
            "hard_limit_ntd": 1000,
        },
    }
    with pytest.raises(ConfigValidationError):
        validate_config(cfg)


def test_intraday_pnl_negative_limit_rejected():
    """Negative limits should fail."""
    cfg = {
        "mode": "sim",
        "symbols": ["2330"],
        "intraday_pnl": {
            "soft_limit_ntd": -100,
            "hard_limit_ntd": 1000,
        },
    }
    with pytest.raises(ConfigValidationError):
        validate_config(cfg)


def test_unknown_top_level_key_does_not_crash():
    """Unknown keys are stripped but don't crash validation."""
    cfg = {
        "mode": "sim",
        "symbols": ["2330"],
        "totally_unknown_key": 42,
    }
    result = validate_config(cfg)
    assert result.mode == "sim"


def test_intraday_pnl_defaults_accepted():
    """Config without intraday_pnl section passes (field is optional)."""
    cfg = {
        "mode": "sim",
        "symbols": ["2330"],
    }
    result = validate_config(cfg)
    assert result.intraday_pnl is None


def test_intraday_pnl_hard_limit_negative_rejected():
    """Negative hard_limit_ntd should fail."""
    cfg = {
        "mode": "sim",
        "symbols": ["2330"],
        "intraday_pnl": {
            "soft_limit_ntd": 500,
            "hard_limit_ntd": -1,
        },
    }
    with pytest.raises(ConfigValidationError):
        validate_config(cfg)


def test_intraday_pnl_equal_soft_and_hard_accepted():
    """soft_limit_ntd == hard_limit_ntd is valid (edge case)."""
    cfg = {
        "mode": "sim",
        "symbols": ["2330"],
        "intraday_pnl": {
            "soft_limit_ntd": 1000,
            "hard_limit_ntd": 1000,
        },
    }
    result = validate_config(cfg)
    assert result.intraday_pnl.soft_limit_ntd == 1000
    assert result.intraday_pnl.hard_limit_ntd == 1000
