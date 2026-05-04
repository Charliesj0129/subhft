"""Tests for HftConfig strict-mode validation (loop_v1 L1)."""

from __future__ import annotations

import pytest

from hft_platform.config.schema import (
    ConfigValidationError,
    HftConfig,
    validate_config,
)


def _base_dict() -> dict:
    return {
        "mode": "sim",
        "broker": "shioaji",
        "symbols": ["TMFR1"],
        "strategy": {
            "id": "demo",
            "module": "hft_platform.strategies.simple_mm",
            "class": "SimpleMarketMaker",
            "params": {},
        },
        "prometheus_port": 9090,
    }


def test_validate_config_loose_strips_unknown_keys():
    """Default behavior keeps backward compat: unknown keys are stripped, not raised."""
    cfg = _base_dict()
    cfg["totally_unknown_key"] = 42
    cfg["another_unknown"] = "x"
    result = validate_config(cfg)  # strict defaults to False
    assert isinstance(result, HftConfig)
    assert result.mode == "sim"


def test_validate_config_strict_rejects_unknown_keys():
    """Strict mode surfaces typos as ConfigValidationError."""
    cfg = _base_dict()
    cfg["stratgy"] = "typo"  # intentional typo of `strategy`
    with pytest.raises(ConfigValidationError) as exc:
        validate_config(cfg, strict=True)
    assert "stratgy" in str(exc.value)


def test_validate_config_strict_accepts_loop_id():
    """Loop-bound configs with the new loop_id field pass strict mode."""
    cfg = _base_dict()
    cfg["loop_id"] = "r47_tmf_v1"
    result = validate_config(cfg, strict=True)
    assert result.loop_id == "r47_tmf_v1"


def test_validate_config_strict_accepts_known_optional_fields():
    """Strict mode does not regress on existing optional fields."""
    cfg = _base_dict()
    cfg["env"] = "prod"
    cfg["paths"] = {"symbols": "config/symbols.yaml"}
    cfg["replay"] = {"start_date": None, "end_date": None}
    cfg["intraday_pnl"] = {
        "soft_limit_ntd": 500,
        "hard_limit_ntd": 1000,
    }
    result = validate_config(cfg, strict=True)
    assert result.env == "prod"


def test_validate_config_strict_lists_all_unknowns():
    """Error message mentions every unknown key, sorted for stability."""
    cfg = _base_dict()
    cfg["zeta"] = 1
    cfg["alpha"] = 2
    cfg["mu"] = 3
    with pytest.raises(ConfigValidationError) as exc:
        validate_config(cfg, strict=True)
    msg = str(exc.value)
    # Sorted: alpha, mu, zeta
    assert msg.index("alpha") < msg.index("mu") < msg.index("zeta")


def test_loop_id_field_default_is_none():
    """Configs without loop_id remain backward compatible."""
    cfg = _base_dict()
    result = validate_config(cfg)
    assert result.loop_id is None
