"""Fubon session lifecycle management.

Implements the BrokerSession protocol for Fubon Neo SDK.
"""

from __future__ import annotations

import time
from typing import Any

from structlog import get_logger

from hft_platform.feed_adapter.fubon._config import FubonClientConfig

logger = get_logger("fubon.session")


def _get_sdk_class() -> Any:
    """Lazy import to avoid hard dependency on fubon-neo package."""
    try:
        from fubon_neo.sdk import FubonSDK  # type: ignore[import-untyped]

        return FubonSDK
    except ImportError as e:
        raise RuntimeError("fubon-neo package not installed. Install with: pip install fubon-neo") from e


class FubonSessionRuntime:
    """Manages Fubon SDK login, reconnection, and session lifecycle.

    Satisfies the ``BrokerSession`` protocol defined in
    ``hft_platform.feed_adapter.protocols``.
    """

    __slots__ = (
        "_config",
        "_sdk",
        "_account",
        "_logged_in",
        "_login_count",
    )

    def __init__(self, config: FubonClientConfig) -> None:
        self._config = config
        self._sdk: Any = None
        self._account: Any = None
        self._logged_in = False
        self._login_count = 0

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def logged_in(self) -> bool:
        """Whether the session is currently authenticated."""
        return self._logged_in

    @property
    def sdk(self) -> Any:
        """The underlying FubonSDK instance. None before login."""
        return self._sdk

    @property
    def account(self) -> Any:
        """The active trading account. None before login."""
        return self._account

    # ------------------------------------------------------------------ #
    # BrokerSession protocol
    # ------------------------------------------------------------------ #

    def login(self, **kwargs: Any) -> Any:
        """Login to Fubon Neo API.

        Uses credentials from config. Keyword args can override:
        - user_id, password, cert_path, cert_password
        """
        cfg = self._config
        user_id = kwargs.get("user_id", cfg.user_id)
        password = kwargs.get("password", cfg.password)
        cert_path = kwargs.get("cert_path", cfg.cert_path)
        cert_password = kwargs.get("cert_password", cfg.cert_password)

        if not user_id or not password:
            raise ValueError("Fubon login requires user_id and password")

        sdk_cls = _get_sdk_class()
        self._sdk = sdk_cls()

        logger.info("fubon_login_start", user_id_masked=user_id[:3] + "***")
        try:
            result = self._sdk.login(user_id, password, cert_path, cert_password)
            if not result or not hasattr(result, "data") or not result.data:
                raise RuntimeError("Fubon login returned no accounts")
            self._account = result.data[0]
            self._logged_in = True
            self._login_count += 1
            logger.info(
                "fubon_login_success",
                login_count=self._login_count,
                accounts=len(result.data),
            )
            return result
        except Exception:
            self._logged_in = False
            logger.exception("fubon_login_failed")
            raise

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        """Reconnect by re-creating SDK and re-logging in.

        Retries up to ``reconnect_max_retries`` with exponential backoff.
        """
        cfg = self._config
        max_retries = cfg.reconnect_max_retries
        backoff = cfg.reconnect_backoff_s

        logger.warning("fubon_reconnect_start", reason=reason, force=force)
        self._logged_in = False

        for attempt in range(1, max_retries + 1):
            try:
                self.close(logout=False)
                self.login()
                logger.info("fubon_reconnect_success", attempt=attempt)
                return True
            except Exception:
                logger.warning(
                    "fubon_reconnect_attempt_failed",
                    attempt=attempt,
                    max_retries=max_retries,
                )
                if attempt < max_retries:
                    time.sleep(backoff * attempt)

        logger.error("fubon_reconnect_exhausted", max_retries=max_retries)
        return False

    def close(self, logout: bool = False) -> None:
        """Close the session, optionally logging out from the SDK."""
        self._logged_in = False
        if self._sdk is not None and logout:
            try:
                if hasattr(self._sdk, "logout"):
                    self._sdk.logout()
            except Exception:
                logger.warning("fubon_logout_error", exc_info=True)
        logger.info("fubon_session_closed", logout=logout)

    def shutdown(self, logout: bool = False) -> None:
        """Full shutdown — close session and release SDK references."""
        self.close(logout=logout)
        self._sdk = None
        self._account = None
        logger.info("fubon_session_shutdown")
