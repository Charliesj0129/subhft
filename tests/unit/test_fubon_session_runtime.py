"""Tests for FubonSessionRuntime and FubonContractsRuntime."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Provide a fake fubon_neo.sdk module so we never need the real SDK installed.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _fake_fubon_neo():
    """Inject a fake fubon_neo.sdk module into sys.modules."""
    fake_sdk_cls = MagicMock(name="FubonSDK")
    fake_module = types.ModuleType("fubon_neo")
    fake_sdk_module = types.ModuleType("fubon_neo.sdk")
    fake_sdk_module.FubonSDK = fake_sdk_cls  # type: ignore[attr-defined]
    fake_module.sdk = fake_sdk_module  # type: ignore[attr-defined]

    saved = {
        "fubon_neo": sys.modules.get("fubon_neo"),
        "fubon_neo.sdk": sys.modules.get("fubon_neo.sdk"),
    }
    sys.modules["fubon_neo"] = fake_module
    sys.modules["fubon_neo.sdk"] = fake_sdk_module

    yield fake_sdk_cls

    # Restore
    for key, val in saved.items():
        if val is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = val


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runtime(monkeypatch, *, personal_id="A123456789", password="pw", api_key="", **extra):
    """Create a FubonSessionRuntime with env vars set."""
    monkeypatch.setenv("FUBON_PERSONAL_ID", personal_id)
    monkeypatch.setenv("FUBON_PASSWORD", password)
    monkeypatch.setenv("FUBON_API_KEY", api_key)
    monkeypatch.setenv("FUBON_CERT_PATH", extra.get("cert_path", "/tmp/cert.pfx"))
    monkeypatch.setenv("FUBON_CERT_PASS", extra.get("cert_pass", "certpw"))

    from hft_platform.feed_adapter.fubon.session_runtime import FubonSessionRuntime

    rt = FubonSessionRuntime()
    # Inject a mock SDK so we never call real broker code.
    rt._sdk = MagicMock(name="sdk_instance")
    return rt


# ===========================================================================
# FubonSessionRuntime tests
# ===========================================================================


class TestFubonSessionRuntimeLogin:
    """Login happy-path and error scenarios."""

    def test_login_with_password(self, monkeypatch):
        rt = _make_runtime(monkeypatch, password="secret")
        assert rt.login() is True
        assert rt.logged_in is True
        rt._sdk.login.assert_called_once_with(
            "A123456789", "secret", "/tmp/cert.pfx", "certpw", account_list=[]
        )

    def test_login_with_api_key(self, monkeypatch):
        rt = _make_runtime(monkeypatch, api_key="my_api_key", password="")
        assert rt.login() is True
        assert rt.logged_in is True
        rt._sdk.apikey_login.assert_called_once_with(
            "A123456789", "my_api_key", "/tmp/cert.pfx", "certpw"
        )

    def test_login_api_key_takes_precedence(self, monkeypatch):
        """When both api_key and password are set, api_key wins."""
        rt = _make_runtime(monkeypatch, api_key="key", password="pw")
        rt.login()
        rt._sdk.apikey_login.assert_called_once()
        rt._sdk.login.assert_not_called()

    def test_login_sdk_raises(self, monkeypatch):
        rt = _make_runtime(monkeypatch)
        rt._sdk.login.side_effect = RuntimeError("broker down")
        assert rt.login() is False
        assert rt.logged_in is False
        assert rt.last_login_error == "broker down"

    def test_login_missing_personal_id(self, monkeypatch):
        monkeypatch.setenv("FUBON_PERSONAL_ID", "")
        monkeypatch.setenv("FUBON_PASSWORD", "pw")
        monkeypatch.setenv("FUBON_API_KEY", "")
        monkeypatch.setenv("FUBON_CERT_PATH", "")
        monkeypatch.setenv("FUBON_CERT_PASS", "")

        from hft_platform.feed_adapter.fubon.session_runtime import FubonSessionRuntime

        rt = FubonSessionRuntime()
        rt._sdk = MagicMock()
        with pytest.raises(ValueError, match="FUBON_PERSONAL_ID"):
            rt.login()

    def test_login_missing_credentials(self, monkeypatch):
        monkeypatch.setenv("FUBON_PERSONAL_ID", "A123")
        monkeypatch.setenv("FUBON_PASSWORD", "")
        monkeypatch.setenv("FUBON_API_KEY", "")
        monkeypatch.setenv("FUBON_CERT_PATH", "")
        monkeypatch.setenv("FUBON_CERT_PASS", "")

        from hft_platform.feed_adapter.fubon.session_runtime import FubonSessionRuntime

        rt = FubonSessionRuntime()
        rt._sdk = MagicMock()
        with pytest.raises(ValueError, match="FUBON_PASSWORD"):
            rt.login()


class TestFubonSessionRuntimeRetry:
    """login_with_retry backoff and exhaustion."""

    def test_retry_succeeds_on_second_attempt(self, monkeypatch):
        rt = _make_runtime(monkeypatch)
        call_count = 0

        def _login_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient")

        rt._sdk.login.side_effect = _login_side_effect

        with patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep") as mock_sleep:
            assert rt.login_with_retry(max_retries=3) is True
            # Should have slept once with 1s backoff
            mock_sleep.assert_called_once_with(1.0)

    def test_retry_exhausted(self, monkeypatch):
        rt = _make_runtime(monkeypatch)
        rt._sdk.login.side_effect = RuntimeError("always fails")

        with patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep"):
            assert rt.login_with_retry(max_retries=3) is False
            assert rt.logged_in is False

    def test_retry_backoff_values(self, monkeypatch):
        rt = _make_runtime(monkeypatch)
        rt._sdk.login.side_effect = RuntimeError("fail")

        with patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep") as mock_sleep:
            rt.login_with_retry(max_retries=3)
            # Backoffs: 1s, 2s (third attempt fails without sleep after)
            assert mock_sleep.call_count == 2
            mock_sleep.assert_any_call(1.0)
            mock_sleep.assert_any_call(2.0)


class TestFubonSessionRuntimeLogout:
    """Logout scenarios."""

    def test_logout(self, monkeypatch):
        rt = _make_runtime(monkeypatch)
        rt.login()
        assert rt.logged_in is True
        rt.logout()
        assert rt.logged_in is False
        rt._sdk.logout.assert_called_once()

    def test_logout_when_not_logged_in(self, monkeypatch):
        rt = _make_runtime(monkeypatch)
        # Should not raise even if never logged in
        rt.logout()
        assert rt.logged_in is False

    def test_logout_sdk_raises(self, monkeypatch):
        rt = _make_runtime(monkeypatch)
        rt.login()
        rt._sdk.logout.side_effect = RuntimeError("oops")
        # Should not propagate the exception
        rt.logout()
        assert rt.logged_in is False


class TestFubonSessionRuntimeConfig:
    """Constructor config and env-var handling."""

    def test_config_dict_overrides_env(self, monkeypatch):
        monkeypatch.setenv("FUBON_PERSONAL_ID", "env_id")
        monkeypatch.setenv("FUBON_PASSWORD", "env_pw")

        from hft_platform.feed_adapter.fubon.session_runtime import FubonSessionRuntime

        rt = FubonSessionRuntime({"personal_id": "cfg_id", "password": "cfg_pw"})
        assert rt._personal_id == "cfg_id"
        assert rt._password == "cfg_pw"


# ===========================================================================
# FubonContractsRuntime tests
# ===========================================================================


class TestFubonContractsRuntime:
    """Contract lookup and symbol validation."""

    def test_get_contract_from_sdk(self):
        from hft_platform.feed_adapter.fubon.contracts_runtime import FubonContractsRuntime

        mock_sdk = MagicMock()
        fake_contract = MagicMock(name="contract_2330")
        mock_sdk.get_contract.return_value = fake_contract

        rt = FubonContractsRuntime(mock_sdk)
        result = rt.get_contract("2330")
        assert result is fake_contract
        mock_sdk.get_contract.assert_called_once_with("2330")

    def test_get_contract_cached(self):
        from hft_platform.feed_adapter.fubon.contracts_runtime import FubonContractsRuntime

        mock_sdk = MagicMock()
        fake_contract = MagicMock()
        mock_sdk.get_contract.return_value = fake_contract

        rt = FubonContractsRuntime(mock_sdk)
        rt.get_contract("2330")
        rt.get_contract("2330")
        # SDK should only be called once; second call hits cache
        mock_sdk.get_contract.assert_called_once()

    def test_get_contract_not_found(self):
        from hft_platform.feed_adapter.fubon.contracts_runtime import FubonContractsRuntime

        mock_sdk = MagicMock()
        mock_sdk.get_contract.return_value = None

        rt = FubonContractsRuntime(mock_sdk)
        assert rt.get_contract("INVALID") is None

    def test_validate_symbols(self):
        from hft_platform.feed_adapter.fubon.contracts_runtime import FubonContractsRuntime

        mock_sdk = MagicMock()

        def _get_contract(symbol):
            return MagicMock() if symbol in {"2330", "2317"} else None

        mock_sdk.get_contract.side_effect = _get_contract

        rt = FubonContractsRuntime(mock_sdk)
        valid = rt.validate_symbols(["2330", "9999", "2317"])
        assert valid == ["2330", "2317"]

    def test_clear_cache(self):
        from hft_platform.feed_adapter.fubon.contracts_runtime import FubonContractsRuntime

        mock_sdk = MagicMock()
        mock_sdk.get_contract.return_value = MagicMock()

        rt = FubonContractsRuntime(mock_sdk)
        rt.get_contract("2330")
        assert rt.cache_size() == 1
        rt.clear_cache()
        assert rt.cache_size() == 0

    def test_sdk_raises_on_lookup(self):
        from hft_platform.feed_adapter.fubon.contracts_runtime import FubonContractsRuntime

        mock_sdk = MagicMock()
        mock_sdk.get_contract.side_effect = RuntimeError("sdk error")

        rt = FubonContractsRuntime(mock_sdk)
        assert rt.get_contract("2330") is None
