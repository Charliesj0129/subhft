"""Tests for FubonSessionRuntime login against the real fubon-neo 2.5.0 login shapes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog

from hft_platform.feed_adapter.fubon.session_runtime import FubonSessionRuntime
from tests.unit.fubon_mock_helper import FakeResult, fail, make_account, make_fubon_sdk_mock, ok

_ENV_NAMES = (
    "HFT_ORDER_MODE",
    "HFT_FUBON_TEST_URL",
    "HFT_FUBON_PERSONAL_ID",
    "HFT_FUBON_API_KEY",
    "HFT_FUBON_PASSWORD",
    "HFT_FUBON_CERT_PATH",
    "HFT_FUBON_CERT_PASS",
    "HFT_FUBON_ACCOUNT",
)


@pytest.fixture(autouse=True)
def live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Login mechanics are exercised in live mode; non-live refusal lives in test_fubon_sdk_safety."""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HFT_ORDER_MODE", "live")


@pytest.fixture()
def account() -> SimpleNamespace:
    return make_account()


@pytest.fixture()
def sdk(account: SimpleNamespace) -> MagicMock:
    sdk = make_fubon_sdk_mock()
    sdk.login.return_value = ok([account])
    sdk.apikey_login.return_value = ok([account])
    return sdk


class TestPasswordLogin:
    def test_password_login_calls_real_login_signature(self, sdk: MagicMock, account: SimpleNamespace) -> None:
        rt = FubonSessionRuntime(sdk)

        assert rt.login(password="pwd", personal_id="A123456789", cert_path="/c.pfx", cert_pass="cp") is True

        sdk.login.assert_called_once_with("A123456789", "pwd", "/c.pfx", "cp")
        sdk.apikey_login.assert_not_called()
        assert rt.is_logged_in is True
        assert rt.account is account

    def test_cert_password_is_optional(self, sdk: MagicMock) -> None:
        rt = FubonSessionRuntime(sdk)

        assert rt.login(password="pwd", personal_id="A123456789", cert_path="/c.pfx") is True

        sdk.login.assert_called_once_with("A123456789", "pwd", "/c.pfx", None)


class TestApiKeyLogin:
    def test_api_key_login_calls_real_apikey_login_signature(self, sdk: MagicMock, account: SimpleNamespace) -> None:
        rt = FubonSessionRuntime(sdk)

        assert rt.login(api_key="KEY", personal_id="A123456789", cert_path="/c.pfx", cert_pass="cp") is True

        sdk.apikey_login.assert_called_once_with("A123456789", "KEY", "/c.pfx", "cp")
        sdk.login.assert_not_called()
        assert rt.account is account

    def test_api_key_wins_when_password_is_also_available(self, sdk: MagicMock) -> None:
        rt = FubonSessionRuntime(sdk)

        rt.login(api_key="KEY", password="pwd", personal_id="A123456789", cert_path="/c.pfx")

        sdk.apikey_login.assert_called_once()
        sdk.login.assert_not_called()


class TestCredentialsFromEnvironment:
    def test_all_credentials_fall_back_to_hft_fubon_env(self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_PERSONAL_ID", "A123456789")
        monkeypatch.setenv("HFT_FUBON_API_KEY", "ENVKEY")
        monkeypatch.setenv("HFT_FUBON_CERT_PATH", "/env.pfx")
        monkeypatch.setenv("HFT_FUBON_CERT_PASS", "envcp")

        assert FubonSessionRuntime(sdk).login() is True

        sdk.apikey_login.assert_called_once_with("A123456789", "ENVKEY", "/env.pfx", "envcp")

    def test_password_env_selects_password_login(self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_PERSONAL_ID", "A123456789")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "envpwd")
        monkeypatch.setenv("HFT_FUBON_CERT_PATH", "/env.pfx")

        assert FubonSessionRuntime(sdk).login() is True

        sdk.login.assert_called_once_with("A123456789", "envpwd", "/env.pfx", None)

    def test_explicit_arguments_override_env(self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_PERSONAL_ID", "ENVID")
        monkeypatch.setenv("HFT_FUBON_API_KEY", "ENVKEY")
        monkeypatch.setenv("HFT_FUBON_CERT_PATH", "/env.pfx")

        FubonSessionRuntime(sdk).login(api_key="ARGKEY", personal_id="ARGID", cert_path="/arg.pfx")

        sdk.apikey_login.assert_called_once_with("ARGID", "ARGKEY", "/arg.pfx", None)


class TestMissingCredentials:
    @pytest.mark.parametrize(
        ("kwargs", "named"),
        [
            ({"api_key": "KEY", "cert_path": "/c.pfx"}, "HFT_FUBON_PERSONAL_ID"),
            ({"api_key": "KEY", "personal_id": "A1"}, "HFT_FUBON_CERT_PATH"),
            ({"personal_id": "A1", "cert_path": "/c.pfx"}, "HFT_FUBON_API_KEY or HFT_FUBON_PASSWORD"),
            ({}, "HFT_FUBON_PERSONAL_ID"),
        ],
    )
    def test_missing_credential_is_named_and_sdk_is_not_called(
        self, sdk: MagicMock, kwargs: dict[str, Any], named: str
    ) -> None:
        rt = FubonSessionRuntime(sdk)

        assert rt.login(**kwargs) is False

        assert named in (rt.last_login_error or "")
        assert rt.is_logged_in is False
        sdk.login.assert_not_called()
        sdk.apikey_login.assert_not_called()


class TestLoginFailures:
    def test_failed_result_returns_false_with_message(self, sdk: MagicMock) -> None:
        sdk.apikey_login.return_value = fail("account locked")
        rt = FubonSessionRuntime(sdk)

        assert rt.login(api_key="KEY", personal_id="A1", cert_path="/c.pfx") is False

        assert "account locked" in (rt.last_login_error or "")
        assert rt.is_logged_in is False
        assert rt.account is None

    def test_sdk_exception_returns_false(self, sdk: MagicMock) -> None:
        sdk.login.side_effect = ConnectionError("network down")
        rt = FubonSessionRuntime(sdk)

        assert rt.login(password="pwd", personal_id="A1", cert_path="/c.pfx") is False

        assert "network down" in (rt.last_login_error or "")
        assert rt.is_logged_in is False

    @pytest.mark.parametrize("answer", [ok([]), ok(None), None, FakeResult(True, data="not-a-list")])
    def test_login_without_accounts_returns_false(self, sdk: MagicMock, answer: Any) -> None:
        sdk.login.return_value = answer
        rt = FubonSessionRuntime(sdk)

        assert rt.login(password="pwd", personal_id="A1", cert_path="/c.pfx") is False

        assert rt.is_logged_in is False
        assert rt.last_login_error

    def test_secret_echoed_by_the_sdk_is_masked_in_the_error(self, sdk: MagicMock) -> None:
        sdk.login.return_value = fail("bad password hunter2 for A123456789")
        rt = FubonSessionRuntime(sdk)

        rt.login(password="hunter2", personal_id="A123456789", cert_path="/c.pfx")

        assert "hunter2" not in (rt.last_login_error or "")
        assert "A123456789" not in (rt.last_login_error or "")
        assert "***" in (rt.last_login_error or "")

    def test_secret_in_an_sdk_exception_is_masked_in_the_error(self, sdk: MagicMock) -> None:
        sdk.apikey_login.side_effect = RuntimeError("rejected key KEY-SECRET-1")
        rt = FubonSessionRuntime(sdk)

        rt.login(api_key="KEY-SECRET-1", personal_id="A1", cert_path="/c.pfx")

        assert "KEY-SECRET-1" not in (rt.last_login_error or "")


class TestAccountSelection:
    def test_single_account_is_selected(self, sdk: MagicMock, account: SimpleNamespace) -> None:
        rt = FubonSessionRuntime(sdk)

        rt.login(password="pwd", personal_id="A1", cert_path="/c.pfx")

        assert rt.account is account

    def test_hft_fubon_account_selects_among_several(self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        first, second = make_account("1111111"), make_account("2222222")
        sdk.login.return_value = ok([first, second])
        monkeypatch.setenv("HFT_FUBON_ACCOUNT", "2222222")
        rt = FubonSessionRuntime(sdk)

        assert rt.login(password="pwd", personal_id="A1", cert_path="/c.pfx") is True

        assert rt.account is second

    def test_configured_account_not_returned_by_login_refuses(
        self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_FUBON_ACCOUNT", "9999999")
        rt = FubonSessionRuntime(sdk)

        assert rt.login(password="pwd", personal_id="A1", cert_path="/c.pfx") is False

        assert "HFT_FUBON_ACCOUNT" in (rt.last_login_error or "")
        assert rt.is_logged_in is False

    def test_several_accounts_without_a_selector_refuse_instead_of_guessing(self, sdk: MagicMock) -> None:
        sdk.login.return_value = ok([make_account("1111111"), make_account("2222222")])
        rt = FubonSessionRuntime(sdk)

        assert rt.login(password="pwd", personal_id="A1", cert_path="/c.pfx") is False

        assert "set HFT_FUBON_ACCOUNT" in (rt.last_login_error or "")
        assert rt.account is None


class TestFutoptAccountSelection:
    """A real login returns a stock account and a futopt account; only a futopt one can trade futures."""

    @staticmethod
    def _login(sdk: MagicMock, accounts: list[Any]) -> tuple[FubonSessionRuntime, bool]:
        sdk.login.return_value = ok(accounts)
        rt = FubonSessionRuntime(sdk)
        return rt, rt.login(password="pwd", personal_id="A1", cert_path="/c.pfx")

    @pytest.mark.parametrize("stock_first", [True, False])
    def test_stock_and_futopt_accounts_without_a_selector_use_the_futopt_account(
        self, sdk: MagicMock, stock_first: bool
    ) -> None:
        stock = make_account("8000001", account_type="stock")
        futopt = make_account("9000002", account_type="futopt")

        rt, logged_in = self._login(sdk, [stock, futopt] if stock_first else [futopt, stock])

        assert logged_in is True
        assert rt.account is futopt

    def test_selector_naming_the_futopt_account_selects_it_among_stock_and_futopt(
        self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stock, futopt = make_account("8000001", account_type="stock"), make_account("9000002")
        monkeypatch.setenv("HFT_FUBON_ACCOUNT", "9000002")

        rt, logged_in = self._login(sdk, [stock, futopt])

        assert logged_in is True
        assert rt.account is futopt

    def test_selector_picks_among_several_futopt_accounts_beside_a_stock_account(
        self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stock = make_account("8000001", account_type="stock")
        first, second = make_account("9000001"), make_account("9000002")
        monkeypatch.setenv("HFT_FUBON_ACCOUNT", "9000002")

        rt, logged_in = self._login(sdk, [stock, first, second])

        assert logged_in is True
        assert rt.account is second

    def test_selector_naming_a_stock_account_refuses_and_does_not_log_in(
        self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stock, futopt = make_account("8000001", account_type="stock"), make_account("9000002")
        monkeypatch.setenv("HFT_FUBON_ACCOUNT", "8000001")

        rt, logged_in = self._login(sdk, [stock, futopt])

        assert logged_in is False
        assert rt.account is None
        assert rt.is_logged_in is False
        assert "HFT_FUBON_ACCOUNT" in (rt.last_login_error or "")
        assert "futopt" in (rt.last_login_error or "")

    def test_selector_shared_by_a_stock_and_a_futopt_account_selects_the_futopt_one(
        self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stock, futopt = make_account("5550001", account_type="stock"), make_account("5550001")
        monkeypatch.setenv("HFT_FUBON_ACCOUNT", "5550001")

        rt, logged_in = self._login(sdk, [stock, futopt])

        assert logged_in is True
        assert rt.account is futopt

    def test_selector_not_returned_by_login_refuses_with_a_stock_and_a_futopt_account(
        self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_FUBON_ACCOUNT", "7777777")

        rt, logged_in = self._login(sdk, [make_account("8000001", account_type="stock"), make_account("9000002")])

        assert logged_in is False
        assert rt.account is None
        assert "HFT_FUBON_ACCOUNT" in (rt.last_login_error or "")

    @pytest.mark.parametrize("with_selector", [False, True])
    def test_only_stock_accounts_refuse_with_or_without_a_selector(
        self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch, with_selector: bool
    ) -> None:
        if with_selector:
            monkeypatch.setenv("HFT_FUBON_ACCOUNT", "8000001")

        rt, logged_in = self._login(sdk, [make_account("8000001", account_type="stock")])

        assert logged_in is False
        assert rt.account is None
        assert "futopt" in (rt.last_login_error or "")

    def test_several_futopt_accounts_beside_a_stock_account_refuse_without_a_selector(self, sdk: MagicMock) -> None:
        accounts = [make_account("8000001", account_type="stock"), make_account("9000001"), make_account("9000002")]

        rt, logged_in = self._login(sdk, accounts)

        assert logged_in is False
        assert rt.account is None
        assert "set HFT_FUBON_ACCOUNT" in (rt.last_login_error or "")
        assert "2 futopt accounts" in (rt.last_login_error or "")

    @pytest.mark.parametrize("account_type", ["futopt", "FutOpt", " futopt "])
    def test_account_type_is_matched_ignoring_case_and_padding(self, sdk: MagicMock, account_type: str) -> None:
        stock = make_account("8000001", account_type="stock")
        futopt = make_account("9000002", account_type=account_type)

        rt, logged_in = self._login(sdk, [stock, futopt])

        assert logged_in is True
        assert rt.account is futopt

    @pytest.mark.parametrize("account_type", ["", "stock", "futopt2", None, 5])
    def test_account_of_another_or_unreadable_type_is_never_selected(self, sdk: MagicMock, account_type: Any) -> None:
        rt, logged_in = self._login(sdk, [make_account("1234567", account_type=account_type)])

        assert logged_in is False
        assert rt.account is None

    def test_account_without_an_account_type_is_never_selected(self, sdk: MagicMock) -> None:
        untyped = SimpleNamespace(name="x", branch_no="6460", account="1234567")

        rt, logged_in = self._login(sdk, [untyped])

        assert logged_in is False
        assert rt.account is None

    def test_refusal_messages_never_contain_an_account_number(
        self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_FUBON_ACCOUNT", "8000001")
        messages = []
        for accounts in (
            [make_account("8000001", account_type="stock"), make_account("9000002")],
            [make_account("8000001", account_type="stock")],
        ):
            rt, _ = self._login(sdk, accounts)
            messages.append(rt.last_login_error or "")

        assert all(messages)
        assert not any(number in message for message in messages for number in ("8000001", "9000002"))


class TestCredentialsAreNeverLogged:
    def test_no_log_event_contains_a_credential(self, sdk: MagicMock) -> None:
        secrets = ("PID-SECRET-1", "KEY-SECRET-2", "CERTPASS-SECRET-3")
        sdk.apikey_login.return_value = fail(f"bad {secrets[1]}")
        rt = FubonSessionRuntime(sdk)

        with structlog.testing.capture_logs() as logs:
            rt.login(api_key=secrets[1], personal_id=secrets[0], cert_path="/c.pfx", cert_pass=secrets[2])
            sdk.apikey_login.return_value = ok([make_account()])
            rt.login(api_key=secrets[1], personal_id=secrets[0], cert_path="/c.pfx", cert_pass=secrets[2])

        assert logs
        rendered = repr(logs)
        for secret in secrets:
            assert secret not in rendered

    def test_snapshot_masks_the_account_number(self, sdk: MagicMock) -> None:
        rt = FubonSessionRuntime(sdk)
        rt.login(password="pwd", personal_id="A1", cert_path="/c.pfx")

        snap = rt.snapshot()

        assert snap["account"] == "***567"
        assert "1234567" not in repr(snap)


class TestSnapshotAndState:
    def test_initial_snapshot(self, sdk: MagicMock) -> None:
        snap = FubonSessionRuntime(sdk).snapshot()

        assert snap["logged_in"] is False
        assert snap["account"] is None
        assert snap["last_login_error"] is None
        assert snap["login_latency_ns"] == 0
        assert snap["sdk_target"] == "vendor_default"

    def test_snapshot_after_login_records_latency(self, sdk: MagicMock) -> None:
        rt = FubonSessionRuntime(sdk)
        rt.login(password="pwd", personal_id="A1", cert_path="/c.pfx")

        snap = rt.snapshot()

        assert snap["logged_in"] is True
        assert snap["login_latency_ns"] > 0

    def test_snapshot_after_failed_login_keeps_the_reason(self, sdk: MagicMock) -> None:
        sdk.login.return_value = fail("nope")
        rt = FubonSessionRuntime(sdk)
        rt.login(password="pwd", personal_id="A1", cert_path="/c.pfx")

        snap = rt.snapshot()

        assert snap["logged_in"] is False
        assert "nope" in snap["last_login_error"]

    def test_failed_relogin_clears_the_logged_in_flag(self, sdk: MagicMock) -> None:
        rt = FubonSessionRuntime(sdk)
        rt.login(password="pwd", personal_id="A1", cert_path="/c.pfx")
        sdk.login.return_value = fail("expired")

        rt.login(password="pwd", personal_id="A1", cert_path="/c.pfx")

        assert rt.is_logged_in is False
