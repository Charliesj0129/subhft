"""Fubon session lifecycle management: login, logout, reconnect with retry.

Mirrors the Shioaji SessionRuntime pattern but adapted for the Fubon SDK
(fubon_neo 2.5.0). The SDK is imported lazily so the module can be loaded even
when fubon_neo is not installed.

Real login shapes (fubon-neo 2.5.0, introspected from the wheel):

- ``login(personal_id, password, cert_path, cert_pass=None)`` (cert + password)
- ``apikey_login(personal_id, api_key, cert_path, cert_pass=None)``
- both return a ``Result`` (``is_success``, ``message``, ``data``) whose ``data``
  is the list of trading ``Account`` objects.

Simulation safety
-----------------
Constructing ``FubonSDK()`` opens the trading websocket, and with no argument it
targets the production endpoint (``wss://neoapi.fbs.com.tw/TASP/XCPXWS``).
``HFT_ORDER_MODE=sim`` (the platform default) does not stop dispatch, so the SDK
target itself is the guard here: unless ``HFT_ORDER_MODE`` is ``live``/``real``,
the SDK is only built against the endpoint named by ``HFT_FUBON_TEST_URL``, and
``FubonSessionRuntime.login`` refuses to authenticate a session that is not
bound to such an endpoint.  A missing, malformed or production-host test URL
fails closed; production is never reached by default.
"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlsplit

import structlog

from hft_platform.feed_adapter.fubon.order_codec import FubonSdkResultError, unwrap_sdk_result

logger = structlog.get_logger("feed_adapter.fubon.session_runtime")

# Default reconnect cooldown in seconds.
_DEFAULT_RECONNECT_COOLDOWN_S: float = 5.0

# Environment variables (credentials come from the environment only).
ENV_ORDER_MODE = "HFT_ORDER_MODE"
ENV_TEST_URL = "HFT_FUBON_TEST_URL"
ENV_PERSONAL_ID = "HFT_FUBON_PERSONAL_ID"
ENV_API_KEY = "HFT_FUBON_API_KEY"
ENV_PASSWORD = "HFT_FUBON_PASSWORD"
ENV_CERT_PATH = "HFT_FUBON_CERT_PATH"
ENV_CERT_PASS = "HFT_FUBON_CERT_PASS"
ENV_ACCOUNT = "HFT_FUBON_ACCOUNT"

# Endpoint fubon_neo connects to when no ``url`` is given.
PROD_TRADING_WS_URL = "wss://neoapi.fbs.com.tw/TASP/XCPXWS"
_PROD_TRADING_HOSTS = frozenset({"neoapi.fbs.com.tw"})

# ``HFT_ORDER_MODE`` values that may use a real broker session (``real`` is
# bootstrap's alias for ``live``).  Anything else, including unset, is not live.
_LIVE_ORDER_MODES = frozenset({"live", "real"})

_SECRET_MASK = "***"
_ACCOUNT_TAIL = 3


class FubonSimSafetyError(RuntimeError):
    """The Fubon SDK would reach production while the platform is not in live mode."""


def is_live_order_mode() -> bool:
    """Return ``True`` only when ``HFT_ORDER_MODE`` is explicitly live (or real)."""
    return os.environ.get(ENV_ORDER_MODE, "").strip().lower() in _LIVE_ORDER_MODES


def validate_test_url(url: str | None) -> str:
    """Return *url* when it is a usable non-production websocket endpoint.

    Raises:
        FubonSimSafetyError: *url* is empty, not a ws/wss URL, or names the
            production trading host.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise FubonSimSafetyError(f"{ENV_TEST_URL} is not set")
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise FubonSimSafetyError(f"{ENV_TEST_URL} is not a valid URL") from exc
    if parts.scheme not in ("ws", "wss") or not host:
        raise FubonSimSafetyError(f"{ENV_TEST_URL} must be a ws:// or wss:// URL with a host")
    if host in _PROD_TRADING_HOSTS:
        raise FubonSimSafetyError(f"{ENV_TEST_URL} names the production trading host")
    return candidate


def resolve_sdk_url() -> str | None:
    """Return the trading endpoint the SDK must be built with.

    ``None`` means "vendor default" and is only returned in live mode.  In any
    other mode the validated ``HFT_FUBON_TEST_URL`` is returned.

    Raises:
        FubonSimSafetyError: not live and no usable test endpoint is configured.
    """
    if is_live_order_mode():
        return None
    try:
        return validate_test_url(os.environ.get(ENV_TEST_URL))
    except FubonSimSafetyError as exc:
        raise FubonSimSafetyError(
            f"{ENV_ORDER_MODE} is not live, so the Fubon SDK may only target the official test "
            f"environment, but {exc}; refusing to construct the SDK (it connects on construction)"
        ) from exc


def build_fubon_sdk(sdk_cls: Any) -> tuple[Any, str | None]:
    """Construct the Fubon SDK against the endpoint the order mode allows.

    Returns:
        ``(sdk, url)`` where ``url`` is the test endpoint, or ``None`` for the vendor
        default (live mode only).  Pass ``url`` to ``FubonSessionRuntime`` as
        ``config["sdk_url"]`` so login can verify the binding.

    Raises:
        FubonSimSafetyError: not live and no usable test endpoint; the SDK
            class is never called in that case.
    """
    url = resolve_sdk_url()
    sdk = sdk_cls() if url is None else sdk_cls(url=url)
    return sdk, url


def _mask_account(account: Any) -> str | None:
    """Return the account number with all but its last digits masked."""
    number = str(getattr(account, "account", "")) if account else ""
    if not number:
        return None
    return _SECRET_MASK + number[-_ACCOUNT_TAIL:]


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    """Mask every non-empty secret inside *text*."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, _SECRET_MASK)
    return text


class FubonSessionRuntime:
    """Full session lifecycle for the Fubon SDK.

    Responsibilities:
      - login / login_with_retry (exponential backoff)
      - logout (graceful disconnect)
      - reconnect (with cooldown guard)
      - refresh_token (re-login; Fubon has no token refresh API)
      - snapshot (introspection dict)

    The class accepts a *sdk* object (``fubon_neo.sdk.FubonSDK`` instance)
    and an optional *config* dict for tuning reconnect cooldown, etc.
    ``config["sdk_url"]`` is the endpoint the SDK was built with (the second
    item of :func:`build_fubon_sdk`); outside live mode login is refused
    unless it names a valid test endpoint.
    """

    __slots__ = (
        "_sdk",
        "_config",
        "_sdk_url",
        "_logged_in",
        "_account",
        "_last_reconnect_ns",
        "_last_login_error",
        "_login_refused",
        "_reconnect_cooldown_s",
        "_login_latency_ns",
    )

    def __init__(self, sdk: Any, config: dict[str, Any] | None = None) -> None:
        self._sdk = sdk
        self._config = config or {}
        self._sdk_url: str | None = self._config.get("sdk_url")
        self._logged_in: bool = False
        self._account: Any = None
        self._last_reconnect_ns: int = 0
        self._last_login_error: str | None = None
        self._login_refused: bool = False
        self._reconnect_cooldown_s: float = float(
            self._config.get("reconnect_cooldown_s", _DEFAULT_RECONNECT_COOLDOWN_S),
        )
        self._login_latency_ns: int = 0

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def is_logged_in(self) -> bool:
        """Return ``True`` if the session is currently authenticated."""
        return self._logged_in

    @property
    def account(self) -> Any:
        """Return the active trading account (set after successful login)."""
        return self._account

    @property
    def last_login_error(self) -> str | None:
        """Return the last login failure reason (never contains credentials)."""
        return self._last_login_error

    # ------------------------------------------------------------------ #
    # Login
    # ------------------------------------------------------------------ #

    def _assert_login_target(self) -> None:
        """Refuse a session that is not bound to an endpoint the order mode allows."""
        if is_live_order_mode():
            return
        try:
            validate_test_url(self._sdk_url)
        except FubonSimSafetyError as exc:
            raise FubonSimSafetyError(
                f"{ENV_ORDER_MODE} is not live and the SDK is not bound to the official test "
                f"environment ({exc}); refusing to log in"
            ) from exc

    @staticmethod
    def _select_account(accounts: Any) -> Any:
        """Pick the trading account from the login result.

        ``HFT_FUBON_ACCOUNT`` selects by account number.  Without it exactly one
        account must exist: guessing among several would trade the wrong book.
        """
        if not isinstance(accounts, (list, tuple)) or not accounts:
            raise FubonSdkResultError("login returned no accounts")
        wanted = os.environ.get(ENV_ACCOUNT, "").strip()
        if wanted:
            for candidate in accounts:
                if str(getattr(candidate, "account", "")) == wanted:
                    return candidate
            raise FubonSdkResultError(f"{ENV_ACCOUNT} is not among the {len(accounts)} accounts returned by login")
        if len(accounts) > 1:
            raise FubonSdkResultError(f"login returned {len(accounts)} accounts; set {ENV_ACCOUNT} to choose one")
        return accounts[0]

    def _fail_login(self, reason: str, start_ns: int | None = None, **fields: Any) -> bool:
        if start_ns is not None:
            self._login_latency_ns = time.perf_counter_ns() - start_ns
            fields["latency_ms"] = round(self._login_latency_ns / 1_000_000, 2)
        self._last_login_error = reason
        self._logged_in = False
        logger.error("fubon_login_failed", reason=reason, **fields)
        return False

    def login(
        self,
        api_key: str | None = None,
        password: str | None = None,
        cert_path: str | None = None,
        *,
        personal_id: str | None = None,
        cert_pass: str | None = None,
    ) -> bool:
        """Authenticate with the Fubon SDK.

        Credentials fall back to environment variables when not provided
        explicitly:
          - ``HFT_FUBON_PERSONAL_ID`` (required)
          - ``HFT_FUBON_CERT_PATH`` (required)
          - ``HFT_FUBON_API_KEY`` (selects ``apikey_login``) or
            ``HFT_FUBON_PASSWORD`` (selects cert + password ``login``)
          - ``HFT_FUBON_CERT_PASS`` (optional certificate password)

        When both an API key and a password are available the API key wins.
        Outside live ``HFT_ORDER_MODE`` the session must be bound to the test
        endpoint, otherwise login is refused (returns ``False``) before any
        credential is used.

        Returns ``True`` on success, ``False`` on failure.
        """
        try:
            self._assert_login_target()
        except FubonSimSafetyError as exc:
            self._login_refused = True
            return self._fail_login(str(exc), refused=True)
        self._login_refused = False

        pid = personal_id or os.environ.get(ENV_PERSONAL_ID, "")
        key = api_key or os.environ.get(ENV_API_KEY, "")
        pwd = password or os.environ.get(ENV_PASSWORD, "")
        cert = cert_path or os.environ.get(ENV_CERT_PATH, "")
        cert_secret = cert_pass or os.environ.get(ENV_CERT_PASS, "") or None

        missing = [name for name, value in ((ENV_PERSONAL_ID, pid), (ENV_CERT_PATH, cert)) if not value]
        if not key and not pwd:
            missing.append(f"{ENV_API_KEY} or {ENV_PASSWORD}")
        if missing:
            return self._fail_login("missing credentials: " + ", ".join(missing))

        method = "apikey_login" if key else "login"
        secrets = (pid, key, pwd, cert_secret or "")
        start_ns = time.perf_counter_ns()
        try:
            if key:
                result = self._sdk.apikey_login(pid, key, cert, cert_secret)
            else:
                result = self._sdk.login(pid, pwd, cert, cert_secret)
            self._login_latency_ns = time.perf_counter_ns() - start_ns
            account = self._select_account(unwrap_sdk_result(result, method))
        except Exception as exc:
            return self._fail_login(_scrub(str(exc), secrets), start_ns, method=method)

        self._account = account
        self._logged_in = True
        self._last_login_error = None
        logger.info(
            "fubon_login_ok",
            method=method,
            latency_ms=round(self._login_latency_ns / 1_000_000, 2),
        )
        return True

    # ------------------------------------------------------------------ #
    # Login with retry
    # ------------------------------------------------------------------ #

    def login_with_retry(
        self,
        max_retries: int = 3,
        backoff_base_s: float = 1.0,
        api_key: str | None = None,
        password: str | None = None,
        cert_path: str | None = None,
        *,
        personal_id: str | None = None,
        cert_pass: str | None = None,
    ) -> bool:
        """Login with exponential backoff retry.

        Tries up to *max_retries* times with delays of
        ``backoff_base_s * 2**attempt`` seconds between attempts
        (1s, 2s, 4s for defaults).  A simulation-safety refusal is final and is
        not retried.

        Returns ``True`` on first successful login.
        """
        for attempt in range(max_retries):
            logger.info("fubon_login_attempt", attempt=attempt + 1, max=max_retries)
            if self.login(
                api_key=api_key,
                password=password,
                cert_path=cert_path,
                personal_id=personal_id,
                cert_pass=cert_pass,
            ):
                return True
            if self._login_refused:
                return False
            if attempt < max_retries - 1:
                sleep_s = backoff_base_s * (2**attempt)
                logger.warning(
                    "fubon_login_retry_backoff",
                    attempt=attempt + 1,
                    sleep_s=sleep_s,
                    error=self._last_login_error,
                )
                time.sleep(sleep_s)

        logger.error(
            "fubon_login_retries_exhausted",
            max_retries=max_retries,
            error=self._last_login_error,
        )
        return False

    # ------------------------------------------------------------------ #
    # Logout
    # ------------------------------------------------------------------ #

    def logout(self) -> None:
        """Graceful SDK disconnect."""
        try:
            self._sdk.logout()
        except Exception as exc:
            logger.warning("fubon_logout_error", error=str(exc))
        self._logged_in = False
        logger.info("fubon_logged_out")

    # ------------------------------------------------------------------ #
    # Reconnect
    # ------------------------------------------------------------------ #

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        """Logout then login_with_retry, respecting a cooldown guard.

        The cooldown (default 5 s) prevents reconnect storms. Set
        *force=True* to bypass it.

        Returns ``True`` on successful reconnect.
        """
        now_ns = time.perf_counter_ns()
        elapsed_s = (now_ns - self._last_reconnect_ns) / 1_000_000_000

        if not force and self._last_reconnect_ns > 0 and elapsed_s < self._reconnect_cooldown_s:
            logger.info(
                "fubon_reconnect_cooldown",
                elapsed_s=round(elapsed_s, 2),
                cooldown_s=self._reconnect_cooldown_s,
                reason=reason,
            )
            return False

        self._last_reconnect_ns = now_ns
        logger.info("fubon_reconnect_start", reason=reason, force=force)
        self.logout()
        return self.login_with_retry()

    # ------------------------------------------------------------------ #
    # Refresh token (re-login; Fubon has no token refresh API)
    # ------------------------------------------------------------------ #

    def refresh_token(self) -> bool:
        """Refresh the session by performing a full logout/login cycle.

        Fubon SDK does not expose a dedicated token-refresh endpoint,
        so a re-login is the only mechanism.
        """
        logger.info("fubon_refresh_token_start")
        self.logout()
        return self.login()

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict[str, Any]:
        """Return a dict describing the current session state."""
        return {
            "logged_in": self._logged_in,
            "account": _mask_account(self._account),
            "last_login_error": self._last_login_error,
            "last_reconnect_ns": self._last_reconnect_ns,
            "reconnect_cooldown_s": self._reconnect_cooldown_s,
            "login_latency_ns": self._login_latency_ns,
            "sdk_target": "test" if self._sdk_url else "vendor_default",
        }
