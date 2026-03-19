from hft_platform.utils.logging import _SENSITIVE_PATTERNS, credential_scrubber


class TestScrubber:
    def test_masks_api_key(self):
        e = {"api_key": "ABC", "event": "login"}
        r = credential_scrubber(None, "info", e)
        assert r["api_key"] == "***"
        assert r["event"] == "login"

    def test_masks_password(self):
        assert credential_scrubber(None, "info", {"password": "x"})["password"] == "***"

    def test_non_sensitive_unchanged(self):
        e = {"symbol": "2330", "price": 100}
        r = credential_scrubber(None, "info", e)
        assert r["symbol"] == "2330"

    def test_patterns_comprehensive(self):
        assert {"api_key", "secret_key", "password", "token"}.issubset(_SENSITIVE_PATTERNS)
