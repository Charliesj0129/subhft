"""Fubon session lifecycle management."""

from __future__ import annotations

import os
import time
from typing import Any

import structlog

from hft_platform.core import timebase

logger = structlog.get_logger("feed_adapter.fubon.session")

_MAX_LOGIN_ATTEMPTS = 3
_BASE_BACKOFF_S = 1.0


class FubonSessionRuntime:
    """Session lifecycle management for Fubon TradeAPI.

    Handles login with retry/backoff, token refresh (via logout/login cycle),
    graceful logout, and reconnect.  Mirrors the SessionRuntime pattern used
    by the Shioaji adapter so the two brokers remain interchangeable.
    """

    __slots__ = (
        "_sdk",
        "_account",
        "_logged_in",
        "_last_login_error",
        "_config",
        "log",
    )

    def __init__(self, sdk: Any, config: dict[str, Any] | None = None) -> None:
        self._sdk: Any = sdk
        self._account: Any = None
        self._logged_in: bool = False
        self._last_login_error: str | None = None
        self._config: dict[str, Any] = config or {}
        self.log = logger

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def is_logged_in(self) -> bool:
        """Return True if the broker session is currently authenticated."""
        return self._logged_in

    # ------------------------------------------------------------------ #
    # Login
    # ------------------------------------------------------------------ #

    def login(self, *args: Any, **kwargs: Any) -> bool:
        """Login to Fubon SDK with retry and exponential backoff.

        Reads ``HFT_FUBON_API_KEY`` and ``HFT_FUBON_PASSWORD`` from the
        environment.  If ``HFT_FUBON_CERT_PATH`` is set the certificate
        path is forwarded to the SDK login call.

        Returns True on success, False after all retries are exhausted.
        """
        api_key = os.environ.get("HFT_FUBON_API_KEY", "")
        password = os.environ.get("HFT_FUBON_PASSWORD", "")
        cert_path = os.environ.get("HFT_FUBON_CERT_PATH", "")

        if not api_key or not password:
            self._last_login_error = "missing HFT_FUBON_API_KEY or HFT_FUBON_PASSWORD"
            self.log.error("fubon_login_failed", error=self._last_login_error)
            return False

        max_attempts = int(self._config.get("login_retry_max", _MAX_LOGIN_ATTEMPTS))

        for attempt in range(1, max_attempts + 1):
            start_ns = time.perf_counter_ns()
            try:
                if cert_path:
                    result = self._sdk.login(
                        api_key,
                        password,
                        cert_path=cert_path,
                    )
                else:
                    result = self._sdk.login(api_key, password)

                elapsed_us = (time.perf_counter_ns() - start_ns) / 1_000
                self.log.info(
                    "fubon_login_ok",
                    attempt=attempt,
                    latency_us=round(elapsed_us, 1),
                )

                # Store first account from response
                accounts = getattr(result, "data", None)
                if accounts:
                    self._account = accounts[0]
                else:
                    self._account = None
                    self.log.warning("fubon_login_no_accounts")

                self._logged_in = True
                self._last_login_error = None
                return True

            except Exception as exc:
                elapsed_us = (time.perf_counter_ns() - start_ns) / 1_000
                self._last_login_error = str(exc)
                self.log.warning(
                    "fubon_login_attempt_failed",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    latency_us=round(elapsed_us, 1),
                    error=self._last_login_error,
                )

                if attempt < max_attempts:
                    backoff_s = _BASE_BACKOFF_S * (2 ** (attempt - 1))
                    time.sleep(backoff_s)

        self.log.error(
            "fubon_login_retries_exhausted",
            attempts=max_attempts,
            error=self._last_login_error,
        )
        return False

    # ------------------------------------------------------------------ #
    # Token refresh (logout/login cycle)
    # ------------------------------------------------------------------ #

    def refresh_token(self) -> bool:
        """Refresh session via logout then login.

        Fubon SDK does not expose an explicit token-refresh API, so this
        performs a full logout/login cycle.
        """
        self.log.info("fubon_token_refresh_start")
        self.logout()
        return self.login()

    # ------------------------------------------------------------------ #
    # Logout
    # ------------------------------------------------------------------ #

    def logout(self) -> None:
        """Gracefully terminate the Fubon SDK session."""
        if not self._logged_in:
            return

        try:
            self._sdk.logout()
            self.log.info("fubon_logout_ok")
        except Exception as exc:
            self.log.warning("fubon_logout_error", error=str(exc))
        finally:
            self._logged_in = False
            self._account = None

    # ------------------------------------------------------------------ #
    # Reconnect
    # ------------------------------------------------------------------ #

    def request_reconnect(self, reason: str = "", force: bool = False) -> bool:
        """Reconnect by performing a logout then login cycle.

        Returns True if the new session is established, False otherwise.
        """
        self.log.info("fubon_reconnect_requested", reason=reason, force=force)
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
            "timestamp_ns": timebase.now_ns(),
        }
