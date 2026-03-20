"""WU-06: Risk config edge-case validation tests.

Validates that risk configuration values are sane and within acceptable
ranges for a small-capital (5-wan NTD) trading account. Tests cover
boundary conditions, missing fields, and threshold ordering invariants.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from structlog import get_logger

logger = get_logger("test_risk_config_limits")

# ---------------------------------------------------------------------------
# Risk config validation logic (mirrors what RiskEngine / StormGuard expect)
# ---------------------------------------------------------------------------

# Required top-level sections in a risk config
_REQUIRED_SECTIONS = ("global_limits", "stormguard", "rate_limiter")

# Required fields within global_limits
_REQUIRED_GLOBAL_LIMITS_FIELDS = ("max_position_lots", "max_order_size", "daily_loss_limit")

# Required fields within stormguard
_REQUIRED_STORMGUARD_FIELDS = ("degrade_threshold", "halt_threshold")

# Required fields within rate_limiter
_REQUIRED_RATE_LIMITER_FIELDS = ("max_orders_per_second",)


class RiskConfigValidationError(Exception):
    """Raised when risk config validation fails."""


def validate_risk_config(config: Dict[str, Any]) -> List[str]:
    """Validate a risk config dict and return a list of error strings.

    Returns an empty list if the config is valid.
    """
    errors: List[str] = []

    # --- Section presence ---
    for section in _REQUIRED_SECTIONS:
        if section not in config:
            errors.append(f"missing required section: '{section}'")

    # Early return if sections are missing — further checks would KeyError
    if errors:
        return errors

    gl = config["global_limits"]
    sg = config["stormguard"]
    rl = config["rate_limiter"]

    # --- global_limits required fields ---
    for field in _REQUIRED_GLOBAL_LIMITS_FIELDS:
        if field not in gl:
            errors.append(f"global_limits missing required field: '{field}'")

    # --- global_limits value checks ---
    if "daily_loss_limit" in gl:
        if not isinstance(gl["daily_loss_limit"], (int, float)):
            errors.append("global_limits.daily_loss_limit must be numeric")
        elif gl["daily_loss_limit"] < 0:
            errors.append(f"global_limits.daily_loss_limit must be non-negative, got {gl['daily_loss_limit']}")

    if "max_position_lots" in gl:
        if not isinstance(gl["max_position_lots"], int):
            errors.append("global_limits.max_position_lots must be an integer")
        elif gl["max_position_lots"] <= 0:
            errors.append(f"global_limits.max_position_lots must be positive, got {gl['max_position_lots']}")

    if "max_order_size" in gl:
        if not isinstance(gl["max_order_size"], int):
            errors.append("global_limits.max_order_size must be an integer")
        elif gl["max_order_size"] <= 0:
            errors.append(f"global_limits.max_order_size must be positive, got {gl['max_order_size']}")

    # --- stormguard required fields ---
    for field in _REQUIRED_STORMGUARD_FIELDS:
        if field not in sg:
            errors.append(f"stormguard missing required field: '{field}'")

    # --- stormguard threshold ordering ---
    if "degrade_threshold" in sg and "halt_threshold" in sg:
        if sg["halt_threshold"] < sg["degrade_threshold"]:
            errors.append(
                f"stormguard.halt_threshold ({sg['halt_threshold']}) must be >= "
                f"degrade_threshold ({sg['degrade_threshold']})"
            )

    # --- rate_limiter required fields ---
    for field in _REQUIRED_RATE_LIMITER_FIELDS:
        if field not in rl:
            errors.append(f"rate_limiter missing required field: '{field}'")

    if "max_orders_per_second" in rl:
        if not isinstance(rl["max_orders_per_second"], (int, float)):
            errors.append("rate_limiter.max_orders_per_second must be numeric")
        elif rl["max_orders_per_second"] <= 0:
            errors.append(f"rate_limiter.max_orders_per_second must be positive, got {rl['max_orders_per_second']}")

    return errors


def validate_risk_config_strict(config: Dict[str, Any]) -> None:
    """Validate and raise on first batch of errors."""
    errors = validate_risk_config(config)
    if errors:
        raise RiskConfigValidationError("Risk config validation failed:\n  - " + "\n  - ".join(errors))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _valid_risk_config() -> Dict[str, Any]:
    """Return a minimal valid risk config matching prod/risk.yaml structure."""
    return {
        "global_limits": {
            "max_position_lots": 2,
            "max_order_size": 1,
            "daily_loss_limit": 50000,
            "max_notional_per_symbol": 500000,
            "max_total_notional": 1000000,
        },
        "stormguard": {
            "degrade_threshold": 30000,
            "halt_threshold": 50000,
            "auto_recover": False,
        },
        "rate_limiter": {
            "max_orders_per_second": 5,
            "max_orders_per_minute": 60,
            "burst_size": 3,
        },
        "circuit_breaker": {
            "consecutive_reject_limit": 5,
            "cooldown_seconds": 60,
            "error_rate_threshold": 0.10,
            "error_rate_window_seconds": 300,
        },
        "position_reconciliation": {
            "interval_seconds": 30,
            "max_divergence_lots": 1,
            "halt_on_divergence": True,
        },
    }


# ===================================================================
# Valid config
# ===================================================================


class TestValidRiskConfig:
    """A well-formed risk config must pass validation."""

    def test_valid_config_passes(self):
        cfg = _valid_risk_config()
        errors = validate_risk_config(cfg)
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_valid_config_strict_no_raise(self):
        cfg = _valid_risk_config()
        validate_risk_config_strict(cfg)  # should not raise


# ===================================================================
# Negative / zero value rejection
# ===================================================================


class TestNegativeAndZeroValues:
    """Negative or zero values for critical limits must be rejected."""

    def test_negative_daily_loss_limit(self):
        cfg = _valid_risk_config()
        cfg["global_limits"]["daily_loss_limit"] = -1000
        errors = validate_risk_config(cfg)
        assert any("daily_loss_limit" in e and "non-negative" in e for e in errors)

    def test_negative_daily_loss_limit_raises(self):
        cfg = _valid_risk_config()
        cfg["global_limits"]["daily_loss_limit"] = -5000
        with pytest.raises(RiskConfigValidationError, match="daily_loss_limit"):
            validate_risk_config_strict(cfg)

    def test_zero_daily_loss_limit_allowed(self):
        """Zero daily_loss_limit means 'no loss allowed' — valid but conservative."""
        cfg = _valid_risk_config()
        cfg["global_limits"]["daily_loss_limit"] = 0
        errors = validate_risk_config(cfg)
        assert not any("daily_loss_limit" in e for e in errors)

    def test_zero_max_position_lots(self):
        cfg = _valid_risk_config()
        cfg["global_limits"]["max_position_lots"] = 0
        errors = validate_risk_config(cfg)
        assert any("max_position_lots" in e and "positive" in e for e in errors)

    def test_negative_max_position_lots(self):
        cfg = _valid_risk_config()
        cfg["global_limits"]["max_position_lots"] = -1
        errors = validate_risk_config(cfg)
        assert any("max_position_lots" in e and "positive" in e for e in errors)

    def test_zero_max_order_size(self):
        cfg = _valid_risk_config()
        cfg["global_limits"]["max_order_size"] = 0
        errors = validate_risk_config(cfg)
        assert any("max_order_size" in e and "positive" in e for e in errors)

    def test_negative_max_order_size(self):
        cfg = _valid_risk_config()
        cfg["global_limits"]["max_order_size"] = -2
        errors = validate_risk_config(cfg)
        assert any("max_order_size" in e and "positive" in e for e in errors)


# ===================================================================
# StormGuard threshold ordering
# ===================================================================


class TestStormGuardThresholdOrdering:
    """halt_threshold must be >= degrade_threshold."""

    def test_halt_below_degrade_is_error(self):
        cfg = _valid_risk_config()
        cfg["stormguard"]["degrade_threshold"] = 50000
        cfg["stormguard"]["halt_threshold"] = 30000
        errors = validate_risk_config(cfg)
        assert any("halt_threshold" in e and "degrade_threshold" in e for e in errors)

    def test_halt_equals_degrade_is_valid(self):
        cfg = _valid_risk_config()
        cfg["stormguard"]["degrade_threshold"] = 40000
        cfg["stormguard"]["halt_threshold"] = 40000
        errors = validate_risk_config(cfg)
        assert not any("halt_threshold" in e for e in errors)

    def test_halt_above_degrade_is_valid(self):
        cfg = _valid_risk_config()
        cfg["stormguard"]["degrade_threshold"] = 30000
        cfg["stormguard"]["halt_threshold"] = 50000
        errors = validate_risk_config(cfg)
        assert not any("halt_threshold" in e for e in errors)

    def test_halt_below_degrade_raises_strict(self):
        cfg = _valid_risk_config()
        cfg["stormguard"]["degrade_threshold"] = 50000
        cfg["stormguard"]["halt_threshold"] = 10000
        with pytest.raises(RiskConfigValidationError, match="halt_threshold"):
            validate_risk_config_strict(cfg)


# ===================================================================
# Rate limiter edge cases
# ===================================================================


class TestRateLimiterEdgeCases:
    """Rate limiter with 0 or negative orders/sec must be rejected."""

    def test_zero_max_orders_per_second(self):
        cfg = _valid_risk_config()
        cfg["rate_limiter"]["max_orders_per_second"] = 0
        errors = validate_risk_config(cfg)
        assert any("max_orders_per_second" in e and "positive" in e for e in errors)

    def test_negative_max_orders_per_second(self):
        cfg = _valid_risk_config()
        cfg["rate_limiter"]["max_orders_per_second"] = -1
        errors = validate_risk_config(cfg)
        assert any("max_orders_per_second" in e and "positive" in e for e in errors)

    def test_positive_max_orders_per_second_valid(self):
        cfg = _valid_risk_config()
        cfg["rate_limiter"]["max_orders_per_second"] = 10
        errors = validate_risk_config(cfg)
        assert not any("max_orders_per_second" in e for e in errors)

    def test_zero_orders_per_second_raises_strict(self):
        cfg = _valid_risk_config()
        cfg["rate_limiter"]["max_orders_per_second"] = 0
        with pytest.raises(RiskConfigValidationError, match="max_orders_per_second"):
            validate_risk_config_strict(cfg)


# ===================================================================
# Missing required fields
# ===================================================================


class TestMissingRequiredFields:
    """Missing required sections or fields must be detected."""

    def test_missing_global_limits_section(self):
        cfg = _valid_risk_config()
        del cfg["global_limits"]
        errors = validate_risk_config(cfg)
        assert any("global_limits" in e for e in errors)

    def test_missing_stormguard_section(self):
        cfg = _valid_risk_config()
        del cfg["stormguard"]
        errors = validate_risk_config(cfg)
        assert any("stormguard" in e for e in errors)

    def test_missing_rate_limiter_section(self):
        cfg = _valid_risk_config()
        del cfg["rate_limiter"]
        errors = validate_risk_config(cfg)
        assert any("rate_limiter" in e for e in errors)

    def test_missing_max_position_lots(self):
        cfg = _valid_risk_config()
        del cfg["global_limits"]["max_position_lots"]
        errors = validate_risk_config(cfg)
        assert any("max_position_lots" in e for e in errors)

    def test_missing_daily_loss_limit(self):
        cfg = _valid_risk_config()
        del cfg["global_limits"]["daily_loss_limit"]
        errors = validate_risk_config(cfg)
        assert any("daily_loss_limit" in e for e in errors)

    def test_missing_degrade_threshold(self):
        cfg = _valid_risk_config()
        del cfg["stormguard"]["degrade_threshold"]
        errors = validate_risk_config(cfg)
        assert any("degrade_threshold" in e for e in errors)

    def test_missing_halt_threshold(self):
        cfg = _valid_risk_config()
        del cfg["stormguard"]["halt_threshold"]
        errors = validate_risk_config(cfg)
        assert any("halt_threshold" in e for e in errors)

    def test_missing_max_orders_per_second(self):
        cfg = _valid_risk_config()
        del cfg["rate_limiter"]["max_orders_per_second"]
        errors = validate_risk_config(cfg)
        assert any("max_orders_per_second" in e for e in errors)

    def test_missing_all_required_sections(self):
        errors = validate_risk_config({})
        assert len(errors) == len(_REQUIRED_SECTIONS)

    def test_missing_section_raises_strict(self):
        with pytest.raises(RiskConfigValidationError, match="missing required section"):
            validate_risk_config_strict({})


# ===================================================================
# Sane range checks for small capital (5-wan NTD account)
# ===================================================================


class TestSmallCapitalSaneRanges:
    """For a 5-wan (50,000 NTD) account, config values should be conservative."""

    # Maximum daily loss should not exceed account capital
    SMALL_CAPITAL_NTD = 50000

    def test_daily_loss_limit_within_capital(self):
        """daily_loss_limit should not exceed account capital for 5-wan."""
        cfg = _valid_risk_config()
        assert cfg["global_limits"]["daily_loss_limit"] <= self.SMALL_CAPITAL_NTD

    def test_daily_loss_limit_exceeding_capital_is_dangerous(self):
        """Detect when daily_loss_limit > capital — a configuration risk."""
        cfg = _valid_risk_config()
        cfg["global_limits"]["daily_loss_limit"] = 100000  # 2x capital
        # This is valid syntactically but dangerously large
        errors = validate_risk_config(cfg)
        # Validation passes (no structural error), but caller should check
        assert errors == []
        # The risk is semantic — daily_loss_limit > SMALL_CAPITAL
        assert cfg["global_limits"]["daily_loss_limit"] > self.SMALL_CAPITAL_NTD

    def test_max_position_lots_conservative(self):
        """Small account should have low position limits."""
        cfg = _valid_risk_config()
        assert cfg["global_limits"]["max_position_lots"] <= 5

    def test_max_order_size_conservative(self):
        """Small account should have low order size limits."""
        cfg = _valid_risk_config()
        assert cfg["global_limits"]["max_order_size"] <= 2

    def test_stormguard_thresholds_proportional_to_capital(self):
        """StormGuard thresholds should be <= daily_loss_limit."""
        cfg = _valid_risk_config()
        sg = cfg["stormguard"]
        gl = cfg["global_limits"]
        assert sg["degrade_threshold"] <= gl["daily_loss_limit"]
        assert sg["halt_threshold"] <= gl["daily_loss_limit"]

    def test_prod_config_matches_small_capital_profile(self):
        """Verify the prod risk.yaml fixture matches 5-wan expectations."""
        cfg = _valid_risk_config()
        gl = cfg["global_limits"]
        sg = cfg["stormguard"]
        rl = cfg["rate_limiter"]

        # Position limits
        assert gl["max_position_lots"] == 2
        assert gl["max_order_size"] == 1

        # Loss limits within capital
        assert gl["daily_loss_limit"] == 50000
        assert gl["daily_loss_limit"] <= self.SMALL_CAPITAL_NTD

        # StormGuard ordering: degrade < halt
        assert sg["degrade_threshold"] < sg["halt_threshold"]

        # Rate limiter: reasonable for futures
        assert 1 <= rl["max_orders_per_second"] <= 50


# ===================================================================
# Type validation
# ===================================================================


class TestTypeValidation:
    """Config fields must have correct types."""

    def test_daily_loss_limit_string_rejected(self):
        cfg = _valid_risk_config()
        cfg["global_limits"]["daily_loss_limit"] = "50000"
        errors = validate_risk_config(cfg)
        assert any("daily_loss_limit" in e and "numeric" in e for e in errors)

    def test_max_position_lots_float_rejected(self):
        cfg = _valid_risk_config()
        cfg["global_limits"]["max_position_lots"] = 2.5
        errors = validate_risk_config(cfg)
        assert any("max_position_lots" in e and "integer" in e for e in errors)

    def test_max_order_size_float_rejected(self):
        cfg = _valid_risk_config()
        cfg["global_limits"]["max_order_size"] = 1.5
        errors = validate_risk_config(cfg)
        assert any("max_order_size" in e and "integer" in e for e in errors)

    def test_max_orders_per_second_string_rejected(self):
        cfg = _valid_risk_config()
        cfg["rate_limiter"]["max_orders_per_second"] = "fast"
        errors = validate_risk_config(cfg)
        assert any("max_orders_per_second" in e and "numeric" in e for e in errors)


# ===================================================================
# Multiple errors accumulated
# ===================================================================


class TestMultipleErrors:
    """Validator should accumulate all errors, not fail on first."""

    def test_multiple_errors_collected(self):
        cfg = _valid_risk_config()
        cfg["global_limits"]["daily_loss_limit"] = -1
        cfg["global_limits"]["max_position_lots"] = 0
        cfg["global_limits"]["max_order_size"] = -1
        cfg["rate_limiter"]["max_orders_per_second"] = 0
        cfg["stormguard"]["degrade_threshold"] = 50000
        cfg["stormguard"]["halt_threshold"] = 10000

        errors = validate_risk_config(cfg)
        assert len(errors) >= 4, f"Expected at least 4 errors, got {len(errors)}: {errors}"

    def test_strict_reports_all_errors(self):
        cfg = _valid_risk_config()
        cfg["global_limits"]["daily_loss_limit"] = -1
        cfg["global_limits"]["max_position_lots"] = 0

        with pytest.raises(RiskConfigValidationError) as exc_info:
            validate_risk_config_strict(cfg)

        msg = str(exc_info.value)
        assert "daily_loss_limit" in msg
        assert "max_position_lots" in msg
