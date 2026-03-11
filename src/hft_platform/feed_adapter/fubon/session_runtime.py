"""Fubon (富邦) session lifecycle: login, retry, refresh, logout.

Mirrors the Shioaji SessionRuntime pattern but adapted for fubon_neo SDK.
All fubon_neo imports are lazy to avoid hard dependency at import time.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("feed_adapter.fubon.session_runtime")

# Env var keys ----------------------------------------------------------------
_ENV_PERSONAL_ID = "FUBON_PERSONAL_ID"
_ENV_PASS_VAR = "FUBON_PASSWORD"
_ENV_KEY_VAR = "FUBON_API_KEY"
_ENV_CERT_PATH = "FUBON_CERT_PATH"
_ENV_CERT_PASS_VAR = "FUBON_CERT_PASS"


class FubonSessionRuntime:
    """Manages Fubon broker session lifecycle.

    Responsibilities:
      - Login (password or API-key mode) with exponential-backoff retry
      - Logout
      - Background session-refresh keepalive thread

    All SDK references are obtained lazily so the module can be imported
    without ``fubon_neo`` installed (useful for tests / CI).
    """

    __slots__ = (
        "_sdk",
        "_logged_in",
        "_personal_id",
        "_password",
        "_api_key",
        "_cert_path",
        "_cert_pass",
        "_last_login_error",
        "_session_refresh_running",
        "_session_refresh_thread",
        "_session_refresh_interval_s",
        "_last_session_refresh_ns",
    )

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._sdk: Any = None
        self._logged_in: bool = False
        self._personal_id: str = str(cfg.get("personal_id") or os.getenv(_ENV_PERSONAL_ID, ""))
        self._password: str = str(cfg.get("password") or os.getenv(_ENV_PASS_VAR, ""))
        self._api_key: str = str(cfg.get("api_key") or os.getenv(_ENV_KEY_VAR, ""))
        self._cert_path: str = str(cfg.get("cert_path") or os.getenv(_ENV_CERT_PATH, ""))
        self._cert_pass: str = str(cfg.get("cert_pass") or os.getenv(_ENV_CERT_PASS_VAR, ""))
        self._last_login_error: str | None = None
        self._session_refresh_running: bool = False
        self._session_refresh_thread: threading.Thread | None = None
        self._session_refresh_interval_s: float = float(cfg.get("session_refresh_interval_s", 3600))
        self._last_session_refresh_ns: int = 0

    # ------------------------------------------------------------------ #
    # SDK factory
    # ------------------------------------------------------------------ #

    def _ensure_sdk(self) -> Any:
        """Lazily instantiate the FubonSDK."""
        if self._sdk is None:
            from fubon_neo.sdk import FubonSDK  # lazy import

            self._sdk = FubonSDK()
        return self._sdk

    @property
    def sdk(self) -> Any:
        """Return the underlying SDK instance (creating it if needed)."""
        return self._ensure_sdk()

    # ------------------------------------------------------------------ #
    # Login
    # ------------------------------------------------------------------ #

    def login(self) -> bool:
        """Login once (no retry). Returns True on success."""
        if not self._personal_id:
            raise ValueError(f"Missing required credential: {_ENV_PERSONAL_ID}")
        if not self._api_key and not self._password:
            raise ValueError(f"Missing credentials: set {_ENV_KEY_VAR} or {_ENV_PASS_VAR}")

        sdk = self._ensure_sdk()
        self._logged_in = False
        self._last_login_error = None

        try:
            if self._api_key:
                logger.info("Fubon login via API key", personal_id=self._personal_id[:4] + "***")
                sdk.apikey_login(
                    self._personal_id,
                    self._api_key,
                    self._cert_path or None,
                    self._cert_pass or None,
                )
            else:
                logger.info("Fubon login via password", personal_id=self._personal_id[:4] + "***")
                sdk.login(
                    self._personal_id,
                    self._password,
                    self._cert_path or None,
                    self._cert_pass or None,
                    account_list=[],
                )
            self._logged_in = True
            self._last_session_refresh_ns = timebase.now_ns()
            logger.info("Fubon login successful")
            return True
        except Exception as exc:
            self._last_login_error = str(exc)
            logger.error("Fubon login failed", error=self._last_login_error)
            return False

    def login_with_retry(self, max_retries: int = 3) -> bool:
        """Login with exponential backoff (1s, 2s, 4s, ...).

        Returns True on first successful attempt, False if all retries exhausted.
        """
        attempts = max(1, max_retries)
        for attempt in range(1, attempts + 1):
            if self.login():
                return True
            if attempt < attempts:
                backoff_s = float(2 ** (attempt - 1))
                logger.warning(
                    "Fubon login retry",
                    attempt=attempt,
                    backoff_s=backoff_s,
                    error=self._last_login_error,
                )
                time.sleep(backoff_s)

        logger.error(
            "Fubon login retries exhausted",
            attempts=attempts,
            error=self._last_login_error,
        )
        return False

    # ------------------------------------------------------------------ #
    # Logout
    # ------------------------------------------------------------------ #

    def logout(self) -> None:
        """Logout from Fubon SDK. Safe to call even if not logged in."""
        if self._sdk is not None:
            try:
                self._sdk.logout()
                logger.info("Fubon logout successful")
            except Exception as exc:
                logger.warning("Fubon logout error", error=str(exc))
        self._logged_in = False

    # ------------------------------------------------------------------ #
    # Session refresh
    # ------------------------------------------------------------------ #

    def start_session_refresh_thread(self) -> None:
        """Start a daemon thread that periodically re-authenticates."""
        if self._session_refresh_running:
            return
        if self._session_refresh_interval_s <= 0:
            return

        self._session_refresh_running = True
        logger.info(
            "Starting Fubon session refresh thread",
            interval_s=self._session_refresh_interval_s,
        )

        def _refresh_loop() -> None:
            while self._session_refresh_running and self._logged_in:
                time.sleep(self._session_refresh_interval_s)
                if not self._session_refresh_running:
                    break
                logger.info("Fubon session refresh: re-authenticating")
                try:
                    self.logout()
                    if self.login():
                        logger.info("Fubon session refresh completed")
                    else:
                        logger.error("Fubon session refresh login failed")
                except Exception as exc:
                    logger.error("Fubon session refresh error", error=str(exc))
            self._session_refresh_running = False

        self._session_refresh_thread = threading.Thread(
            target=_refresh_loop,
            name="fubon-session-refresh",
            daemon=True,
        )
        self._session_refresh_thread.start()

    def stop_session_refresh_thread(self) -> None:
        """Signal the refresh thread to stop."""
        self._session_refresh_running = False

    # ------------------------------------------------------------------ #
    # Accessors
    # ------------------------------------------------------------------ #

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    @property
    def last_login_error(self) -> str | None:
        return self._last_login_error
