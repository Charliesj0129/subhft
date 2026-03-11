"""Tests for Fubon session runtime — BrokerSession protocol conformance."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.fubon._config import FubonClientConfig, load_fubon_config
from hft_platform.feed_adapter.fubon.session_runtime import FubonSessionRuntime


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def config() -> FubonClientConfig:
    return FubonClientConfig(
        user_id="A123456789",
        password="secret",
        cert_path="/path/to/cert",
        cert_password="certpw",
        simulation=True,
        reconnect_max_retries=3,
        reconnect_backoff_s=0.01,
    )


@pytest.fixture()
def mock_sdk() -> MagicMock:
    sdk = MagicMock()
    result = MagicMock()
    result.data = [MagicMock(name="account_0")]
    sdk.login.return_value = result
    return sdk


def _patch_sdk(mock_sdk: MagicMock):
    """Return a context manager that patches _get_sdk_class to return a factory."""
    return patch(
        "hft_platform.feed_adapter.fubon.session_runtime._get_sdk_class",
        return_value=lambda: mock_sdk,
    )


# ── BrokerSession protocol conformance ───────────────────────────────


class TestBrokerSessionProtocol:
    def test_isinstance_check(self, config: FubonClientConfig) -> None:
        from hft_platform.feed_adapter.protocols import BrokerSession

        runtime = FubonSessionRuntime(config)
        assert isinstance(runtime, BrokerSession)

    def test_has_all_protocol_methods(self, config: FubonClientConfig) -> None:
        runtime = FubonSessionRuntime(config)
        assert callable(runtime.login)
        assert callable(runtime.reconnect)
        assert callable(runtime.close)
        assert callable(runtime.shutdown)
        # logged_in is a property
        assert isinstance(type(runtime).logged_in, property)


# ── Login ─────────────────────────────────────────────────────────────


class TestLogin:
    def test_login_success(
        self, config: FubonClientConfig, mock_sdk: MagicMock
    ) -> None:
        runtime = FubonSessionRuntime(config)
        with _patch_sdk(mock_sdk):
            result = runtime.login()

        assert runtime.logged_in is True
        assert runtime.account is not None
        assert runtime.sdk is not None
        mock_sdk.login.assert_called_once_with(
            "A123456789", "secret", "/path/to/cert", "certpw"
        )
        assert result is mock_sdk.login.return_value

    def test_login_kwargs_override(
        self, config: FubonClientConfig, mock_sdk: MagicMock
    ) -> None:
        runtime = FubonSessionRuntime(config)
        with _patch_sdk(mock_sdk):
            runtime.login(user_id="B999", password="pw2")

        mock_sdk.login.assert_called_once_with("B999", "pw2", "/path/to/cert", "certpw")
        assert runtime.logged_in is True

    def test_login_failure_raises(
        self, config: FubonClientConfig, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.login.side_effect = ConnectionError("network error")
        runtime = FubonSessionRuntime(config)
        with _patch_sdk(mock_sdk), pytest.raises(ConnectionError, match="network error"):
            runtime.login()
        assert runtime.logged_in is False

    def test_login_no_accounts_raises(
        self, config: FubonClientConfig, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.login.return_value.data = []
        runtime = FubonSessionRuntime(config)
        with _patch_sdk(mock_sdk), pytest.raises(RuntimeError, match="no accounts"):
            runtime.login()
        assert runtime.logged_in is False

    def test_login_missing_credentials_raises(self) -> None:
        cfg = FubonClientConfig(user_id="", password="")
        runtime = FubonSessionRuntime(cfg)
        with pytest.raises(ValueError, match="user_id and password"):
            runtime.login()

    def test_login_missing_password_raises(self) -> None:
        cfg = FubonClientConfig(user_id="A123", password="")
        runtime = FubonSessionRuntime(cfg)
        with pytest.raises(ValueError, match="user_id and password"):
            runtime.login()

    def test_login_count_increments(
        self, config: FubonClientConfig, mock_sdk: MagicMock
    ) -> None:
        runtime = FubonSessionRuntime(config)
        with _patch_sdk(mock_sdk):
            runtime.login()
            runtime.login()
        assert runtime._login_count == 2


# ── Reconnect ─────────────────────────────────────────────────────────


class TestReconnect:
    def test_reconnect_success(
        self, config: FubonClientConfig, mock_sdk: MagicMock
    ) -> None:
        runtime = FubonSessionRuntime(config)
        with _patch_sdk(mock_sdk):
            runtime.login()
            result = runtime.reconnect(reason="test")

        assert result is True
        assert runtime.logged_in is True

    def test_reconnect_exhausts_retries(
        self, config: FubonClientConfig, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.login.side_effect = ConnectionError("fail")
        runtime = FubonSessionRuntime(config)
        with _patch_sdk(mock_sdk):
            result = runtime.reconnect(reason="test")

        assert result is False
        assert runtime.logged_in is False

    def test_reconnect_succeeds_after_failures(
        self, config: FubonClientConfig, mock_sdk: MagicMock
    ) -> None:
        ok_result = MagicMock()
        ok_result.data = [MagicMock()]
        # Fail twice, then succeed
        mock_sdk.login.side_effect = [
            ConnectionError("fail1"),
            ConnectionError("fail2"),
            ok_result,
        ]
        runtime = FubonSessionRuntime(config)
        with _patch_sdk(mock_sdk):
            result = runtime.reconnect(reason="recovery")

        assert result is True
        assert runtime.logged_in is True


# ── Close / Shutdown ──────────────────────────────────────────────────


class TestCloseShutdown:
    def test_close_sets_logged_in_false(
        self, config: FubonClientConfig, mock_sdk: MagicMock
    ) -> None:
        runtime = FubonSessionRuntime(config)
        with _patch_sdk(mock_sdk):
            runtime.login()
        assert runtime.logged_in is True
        runtime.close()
        assert runtime.logged_in is False

    def test_close_with_logout_calls_sdk_logout(
        self, config: FubonClientConfig, mock_sdk: MagicMock
    ) -> None:
        runtime = FubonSessionRuntime(config)
        with _patch_sdk(mock_sdk):
            runtime.login()
        runtime.close(logout=True)
        mock_sdk.logout.assert_called_once()

    def test_close_without_logout_skips_sdk_logout(
        self, config: FubonClientConfig, mock_sdk: MagicMock
    ) -> None:
        runtime = FubonSessionRuntime(config)
        with _patch_sdk(mock_sdk):
            runtime.login()
        runtime.close(logout=False)
        mock_sdk.logout.assert_not_called()

    def test_shutdown_clears_sdk_and_account(
        self, config: FubonClientConfig, mock_sdk: MagicMock
    ) -> None:
        runtime = FubonSessionRuntime(config)
        with _patch_sdk(mock_sdk):
            runtime.login()
        assert runtime.sdk is not None
        assert runtime.account is not None

        runtime.shutdown()
        assert runtime.sdk is None
        assert runtime.account is None
        assert runtime.logged_in is False

    def test_shutdown_with_logout(
        self, config: FubonClientConfig, mock_sdk: MagicMock
    ) -> None:
        runtime = FubonSessionRuntime(config)
        with _patch_sdk(mock_sdk):
            runtime.login()
        runtime.shutdown(logout=True)
        mock_sdk.logout.assert_called_once()
        assert runtime.sdk is None


# ── Initial state ─────────────────────────────────────────────────────


class TestInitialState:
    def test_initial_logged_in_false(self, config: FubonClientConfig) -> None:
        runtime = FubonSessionRuntime(config)
        assert runtime.logged_in is False

    def test_initial_sdk_none(self, config: FubonClientConfig) -> None:
        runtime = FubonSessionRuntime(config)
        assert runtime.sdk is None

    def test_initial_account_none(self, config: FubonClientConfig) -> None:
        runtime = FubonSessionRuntime(config)
        assert runtime.account is None


# ── Config loader ─────────────────────────────────────────────────────


class TestFubonConfig:
    def test_load_from_dict(self) -> None:
        cfg = load_fubon_config(
            {"fubon": {"user_id": "X123", "password": "pw", "simulation": False}}
        )
        assert cfg.user_id == "X123"
        assert cfg.password == "pw"
        assert cfg.simulation is False

    def test_load_defaults(self) -> None:
        cfg = load_fubon_config()
        assert cfg.user_id == ""
        assert cfg.simulation is True
        assert cfg.reconnect_max_retries == 5
        assert cfg.reconnect_backoff_s == 2.0

    def test_load_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FUBON_ID", "ENV_USER")
        monkeypatch.setenv("FUBON_PASSWORD", "ENV_PW")
        monkeypatch.setenv("FUBON_SIMULATION", "0")
        cfg = load_fubon_config()
        assert cfg.user_id == "ENV_USER"
        assert cfg.password == "ENV_PW"
        assert cfg.simulation is False

    def test_frozen(self) -> None:
        cfg = FubonClientConfig()
        with pytest.raises(AttributeError):
            cfg.user_id = "new"  # type: ignore[misc]
