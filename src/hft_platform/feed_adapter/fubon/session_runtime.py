"""Fubon session lifecycle management: login, logout, reconnect with retry.

Mirrors the Shioaji SessionRuntime pattern but adapted for the Fubon SDK
(fubon_neo). The SDK is imported lazily so the module can be loaded even
when fubon_neo is not installed.
"""

from __future__ import annotations

import os
import time
from typing import Any

import structlog

logger = structlog.get_logger("feed_adapter.fubon.session_runtime")

# Default reconnect cooldown in seconds.
_DEFAULT_RECONNECT_COOLDOWN_S: float = 5.0


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
    """

    __slots__ = (
        "_sdk",
        "_config",
        "_logged_in",
        "_account",
        "_last_reconnect_ns",
        "_last_login_error",
        "_reconnect_cooldown_s",
        "_login_latency_ns",
    )

    def __init__(self, sdk: Any, config: dict[str, Any] | None = None) -> None:
        self._sdk = sdk
        self._config = config or {}
        self._logged_in: bool = False
        self._account: Any = None
        self._last_reconnect_ns: int = 0
        self._last_login_error: str | None = None
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

    # ------------------------------------------------------------------ #
    # Login
    # ------------------------------------------------------------------ #

    def login(
        self,
        api_key: str | None = None,
        password: str | None = None,
        cert_path: str | None = None,
    ) -> bool:
        """Authenticate with Fubon SDK.

        Credentials fall back to environment variables when not provided
        explicitly:
          - ``HFT_FUBON_API_KEY``
          - ``HFT_FUBON_PASSWORD``
          - ``HFT_FUBON_CERT_PATH``

        Returns ``True`` on success, ``False`` on failure.
        """
        key = api_key or os.environ.get("HFT_FUBON_API_KEY", "")
        pwd = password or os.environ.get("HFT_FUBON_PASSWORD", "")
        cert = cert_path or os.environ.get("HFT_FUBON_CERT_PATH", "")

        if not key or not pwd:
            self._last_login_error = "missing credentials"
            logger.error("fubon_login_failed", reason=self._last_login_error)
            return False

        start_ns = time.perf_counter_ns()
        try:
            accounts = self._sdk.login(key, pwd, cert)
            self._login_latency_ns = time.perf_counter_ns() - start_ns

            if not accounts or not getattr(accounts, "data", None):
                self._last_login_error = "no accounts returned"
                self._logged_in = False
                logger.error("fubon_login_failed", reason=self._last_login_error)
                return False

            self._account = accounts.data[0]
            self._logged_in = True
            self._last_login_error = None
            logger.info(
                "fubon_login_ok",
                latency_ms=round(self._login_latency_ns / 1_000_000, 2),
            )
            return True
        except Exception as exc:
            self._login_latency_ns = time.perf_counter_ns() - start_ns
            self._last_login_error = str(exc)
            self._logged_in = False
            logger.error(
                "fubon_login_failed",
                error=self._last_login_error,
                latency_ms=round(self._login_latency_ns / 1_000_000, 2),
            )
            return False

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
    ) -> bool:
        """Login with exponential backoff retry.

        Tries up to *max_retries* times with delays of
        ``backoff_base_s * 2**attempt`` seconds between attempts
        (1s, 2s, 4s for defaults).

        Returns ``True`` on first successful login.
        """
        for attempt in range(max_retries):
            logger.info("fubon_login_attempt", attempt=attempt + 1, max=max_retries)
            if self.login(api_key=api_key, password=password, cert_path=cert_path):
                return True
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
            "account": str(self._account) if self._account else None,
            "last_login_error": self._last_login_error,
            "last_reconnect_ns": self._last_reconnect_ns,
            "reconnect_cooldown_s": self._reconnect_cooldown_s,
            "login_latency_ns": self._login_latency_ns,
        }
