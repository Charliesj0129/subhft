"""Tests for startup secret validation."""

import os
from unittest.mock import patch

from hft_platform.core.secret_validator import validate_secrets


class TestSecretValidator:
    def test_missing_shioaji_key_returns_error(self):
        with patch.dict(os.environ, {}, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
            assert any("SHIOAJI_API_KEY" in e for e in errors)

    def test_placeholder_value_returns_error(self):
        env = {"SHIOAJI_API_KEY": "changeme", "SHIOAJI_SECRET_KEY": "real_key"}
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
            assert any("Placeholder" in e for e in errors)

    def test_valid_secrets_pass(self):
        env = {"SHIOAJI_API_KEY": "real_api_key_123", "SHIOAJI_SECRET_KEY": "real_secret_456"}
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
            assert errors == []

    def test_fubon_broker_checks_fubon_keys(self):
        env = {"HFT_BROKER": "fubon"}
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=False)
            assert any("HFT_FUBON_API_KEY" in e for e in errors)

    def test_infra_secrets_validated_when_required(self):
        env = {"SHIOAJI_API_KEY": "key", "SHIOAJI_SECRET_KEY": "secret"}
        with patch.dict(os.environ, env, clear=True):
            errors = validate_secrets(require_broker=True, require_infra=True)
            assert any("CLICKHOUSE_PASSWORD" in e for e in errors)

    def test_no_broker_validation_when_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            errors = validate_secrets(require_broker=False, require_infra=False)
            assert errors == []
