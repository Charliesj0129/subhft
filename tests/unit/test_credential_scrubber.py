"""Tests for structlog credential scrubbing (WU-07)."""

from hft_platform.utils.logging import _SENSITIVE_PATTERNS, credential_scrubber


class TestCredentialScrubber:
    """WU-07: Verify credential scrubbing in log processor."""

    def test_masks_api_key(self):
        event = {"api_key": "ABC123", "event": "login"}
        result = credential_scrubber(None, "info", event)

        assert result["api_key"] == "***"
        assert result["event"] == "login"

    def test_masks_secret_key(self):
        event = {"secret_key": "super_secret_value", "symbol": "2330"}
        result = credential_scrubber(None, "info", event)

        assert result["secret_key"] == "***"
        assert result["symbol"] == "2330"

    def test_masks_password(self):
        event = {"password": "p@ssw0rd!", "user": "admin"}
        result = credential_scrubber(None, "info", event)

        assert result["password"] == "***"
        assert result["user"] == "admin"

    def test_masks_token(self):
        event = {"auth_token": "eyJhbGciOiJIUzI1NiJ9", "status": "ok"}
        result = credential_scrubber(None, "info", event)

        assert result["auth_token"] == "***"
        assert result["status"] == "ok"

    def test_masks_cert_path(self):
        event = {"cert_path": "/etc/ssl/cert.pem"}
        result = credential_scrubber(None, "info", event)

        assert result["cert_path"] == "***"

    def test_non_sensitive_unchanged(self):
        event = {"symbol": "2330", "price": 100, "qty": 10}
        result = credential_scrubber(None, "info", event)

        assert result["symbol"] == "2330"
        assert result["price"] == 100
        assert result["qty"] == 10

    def test_case_insensitive_matching(self):
        event = {"API_KEY": "key123", "Secret_Key": "secret123"}
        result = credential_scrubber(None, "info", event)

        assert result["API_KEY"] == "***"
        assert result["Secret_Key"] == "***"

    def test_partial_match_in_key_name(self):
        event = {"shioaji_api_key_prefix": "ABC"}
        result = credential_scrubber(None, "info", event)

        assert result["shioaji_api_key_prefix"] == "***"

    def test_empty_event_dict(self):
        event: dict = {}
        result = credential_scrubber(None, "info", event)

        assert result == {}

    def test_sensitive_patterns_are_comprehensive(self):
        required = {"api_key", "secret_key", "password", "token", "cert_path", "secret"}
        assert required.issubset(_SENSITIVE_PATTERNS)
