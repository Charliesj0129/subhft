"""Tests for src/hft_platform/core/secret_validator.py — all code paths."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from hft_platform.core.secret_validator import (
    _PLACEHOLDER_VALUES,
    SecretValidationError,
    validate_secrets,
    validate_secrets_for_mode,
)

# ---------------------------------------------------------------------------
# validate_secrets — low-level
# ---------------------------------------------------------------------------


class TestValidateSecrets:
    """Unit tests for :func:`validate_secrets`."""

    def test_all_secrets_present_shioaji(self) -> None:
        env = {
            "SHIOAJI_API_KEY": "real_key_123",
            "SHIOAJI_SECRET_KEY": "real_secret_456",
            "HFT_BROKER": "shioaji",
        }
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
        assert errors == []

    def test_missing_shioaji_api_key(self) -> None:
        env = {
            "SHIOAJI_SECRET_KEY": "real_secret_456",
            "HFT_BROKER": "shioaji",
        }
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
        assert len(errors) == 1
        assert "SHIOAJI_API_KEY" in errors[0]

    def test_missing_shioaji_secret_key(self) -> None:
        env = {
            "SHIOAJI_API_KEY": "real_key_123",
            "HFT_BROKER": "shioaji",
        }
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
        assert len(errors) == 1
        assert "SHIOAJI_SECRET_KEY" in errors[0]

    def test_both_shioaji_keys_missing(self) -> None:
        env = {"HFT_BROKER": "shioaji"}
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
        assert len(errors) == 2

    @pytest.mark.parametrize("placeholder", sorted(_PLACEHOLDER_VALUES - {""}))
    def test_placeholder_shioaji_api_key(self, placeholder: str) -> None:
        env = {
            "SHIOAJI_API_KEY": placeholder,
            "SHIOAJI_SECRET_KEY": "real_secret_456",
            "HFT_BROKER": "shioaji",
        }
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
        assert len(errors) == 1
        assert "Placeholder" in errors[0]

    def test_placeholder_case_insensitive_uppercase(self) -> None:
        """Placeholder detection is case-insensitive (via .lower())."""
        env = {
            "SHIOAJI_API_KEY": "YOUR_KEY",
            "SHIOAJI_SECRET_KEY": "real_secret",
            "HFT_BROKER": "shioaji",
        }
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
        assert len(errors) == 1
        assert "Placeholder" in errors[0]

    def test_fubon_broker_validates_fubon_keys(self) -> None:
        env = {"HFT_BROKER": "fubon"}
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
        assert len(errors) == 2
        assert any("HFT_FUBON_API_KEY" in e for e in errors)
        assert any("HFT_FUBON_PASSWORD" in e for e in errors)

    def test_fubon_keys_present(self) -> None:
        env = {
            "HFT_BROKER": "fubon",
            "HFT_FUBON_API_KEY": "fubon_key_abc",
            "HFT_FUBON_PASSWORD": "fubon_pass_xyz",
        }
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
        assert errors == []

    def test_require_infra_validates_clickhouse_and_redis(self) -> None:
        env = {"HFT_BROKER": "shioaji"}
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=False, require_infra=True)
        assert len(errors) == 2
        assert any("CLICKHOUSE_PASSWORD" in e for e in errors)
        assert any("REDIS_PASSWORD" in e for e in errors)

    def test_infra_secrets_present(self) -> None:
        env = {
            "CLICKHOUSE_PASSWORD": "ch_pass",
            "REDIS_PASSWORD": "redis_pass",
        }
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=False, require_infra=True)
        assert errors == []

    def test_no_checks_when_both_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            errors = validate_secrets(require_broker=False, require_infra=False)
        assert errors == []

    def test_placeholder_case_insensitive_changeme(self) -> None:
        """CHANGEME (uppercase) should also be caught."""
        env = {
            "SHIOAJI_API_KEY": "CHANGEME",
            "SHIOAJI_SECRET_KEY": "real_secret",
            "HFT_BROKER": "shioaji",
        }
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
        assert len(errors) == 1
        assert "Placeholder" in errors[0]

    def test_empty_string_treated_as_missing(self) -> None:
        env = {
            "SHIOAJI_API_KEY": "",
            "SHIOAJI_SECRET_KEY": "real_secret",
            "HFT_BROKER": "shioaji",
        }
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
        assert len(errors) == 1
        assert "Missing" in errors[0]

    def test_default_broker_is_shioaji(self) -> None:
        """When HFT_BROKER is not set, defaults to shioaji."""
        with patch.dict(os.environ, {}, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
        assert any("SHIOAJI_API_KEY" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_secrets_for_mode — mode-aware wrapper
# ---------------------------------------------------------------------------


class TestValidateSecretsForMode:
    """Tests for :func:`validate_secrets_for_mode`."""

    def test_live_mode_raises_on_missing_secrets(self) -> None:
        env = {"HFT_BROKER": "shioaji"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SecretValidationError) as exc_info:
                validate_secrets_for_mode(mode="live")
        assert len(exc_info.value.errors) == 2

    def test_live_mode_raises_on_placeholder_secrets(self) -> None:
        env = {
            "HFT_BROKER": "shioaji",
            "SHIOAJI_API_KEY": "YOUR_API_KEY",
            "SHIOAJI_SECRET_KEY": "changeme",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SecretValidationError) as exc_info:
                validate_secrets_for_mode(mode="live")
        assert len(exc_info.value.errors) == 2

    def test_live_mode_passes_with_valid_secrets(self) -> None:
        env = {
            "HFT_BROKER": "shioaji",
            "SHIOAJI_API_KEY": "real_key_123",
            "SHIOAJI_SECRET_KEY": "real_secret_456",
        }
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets_for_mode(mode="live")
        assert errors == []

    def test_sim_mode_warns_but_does_not_raise(self) -> None:
        env = {"HFT_BROKER": "shioaji"}
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets_for_mode(mode="sim")
        assert len(errors) == 2

    def test_replay_mode_warns_but_does_not_raise(self) -> None:
        env = {"HFT_BROKER": "shioaji"}
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets_for_mode(mode="replay")
        assert len(errors) == 2

    def test_mode_defaults_to_env_var(self) -> None:
        env = {
            "HFT_MODE": "live",
            "HFT_BROKER": "shioaji",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SecretValidationError):
                validate_secrets_for_mode()

    def test_mode_defaults_to_sim_when_no_env(self) -> None:
        env = {"HFT_BROKER": "shioaji"}
        with patch.dict(os.environ, env, clear=True):
            # Should not raise (sim mode default)
            errors = validate_secrets_for_mode()
        assert len(errors) >= 1

    def test_live_fubon_missing_raises(self) -> None:
        env = {"HFT_BROKER": "fubon"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SecretValidationError) as exc_info:
                validate_secrets_for_mode(mode="live")
        assert any("HFT_FUBON_API_KEY" in e for e in exc_info.value.errors)

    def test_require_infra_forwarded(self) -> None:
        env = {
            "HFT_BROKER": "shioaji",
            "SHIOAJI_API_KEY": "real_key",
            "SHIOAJI_SECRET_KEY": "real_secret",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SecretValidationError):
                validate_secrets_for_mode(mode="live", require_infra=True)

    def test_require_broker_false_skips_broker_check(self) -> None:
        env = {"HFT_BROKER": "shioaji"}
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets_for_mode(mode="live", require_broker=False, require_infra=False)
        assert errors == []


# ---------------------------------------------------------------------------
# SecretValidationError
# ---------------------------------------------------------------------------


class TestSecretValidationError:
    """Tests for the error type."""

    def test_error_message_contains_all_errors(self) -> None:
        err = SecretValidationError(["missing A", "placeholder B"])
        assert "missing A" in str(err)
        assert "placeholder B" in str(err)

    def test_errors_attribute(self) -> None:
        errs = ["err1", "err2"]
        exc = SecretValidationError(errs)
        assert exc.errors is errs

    def test_is_runtime_error(self) -> None:
        assert issubclass(SecretValidationError, RuntimeError)
