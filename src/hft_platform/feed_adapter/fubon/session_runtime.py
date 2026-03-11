"""Fubon session runtime — login, reconnect, shutdown."""

from __future__ import annotations

from typing import Any

from structlog import get_logger

from hft_platform.feed_adapter.fubon._config import FubonClientConfig

logger = get_logger("fubon.session")


def _get_sdk_class() -> Any:
    """Lazily import FubonSDK to avoid hard dependency at import time."""
    try:
        from fubon_neo.sdk import FubonSDK  # type: ignore[import-untyped]

        return FubonSDK
    except ImportError as e:
        raise RuntimeError("fubon-neo not installed") from e


class FubonSessionRuntime:
    """Manages Fubon SDK lifecycle: login, reconnect, shutdown."""

    __slots__ = ("_config", "_sdk", "_account", "_logged_in", "_login_count")

    def __init__(self, config: FubonClientConfig) -> None:
        self._config = config
        self._sdk: Any = None
        self._account: Any = None
        self._logged_in = False
        self._login_count = 0

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    @property
    def sdk(self) -> Any:
        return self._sdk

    @property
    def account(self) -> Any:
        return self._account

    def login(self, **kwargs: Any) -> Any:
        """Authenticate with Fubon SDK."""
        cfg = self._config
        uid = kwargs.get("user_id", cfg.user_id)
        pw = kwargs.get("password", cfg.password)
        if not uid or not pw:
            raise ValueError("Fubon login requires user_id and password")
        sdk_cls = _get_sdk_class()
        self._sdk = sdk_cls()
        result = self._sdk.login(uid, pw, cfg.cert_path, cfg.cert_password)
        if not result or not hasattr(result, "data") or not result.data:
            raise RuntimeError("Fubon login returned no accounts")
        self._account = result.data[0]
        self._logged_in = True
        self._login_count += 1
        logger.info("fubon_login_ok", login_count=self._login_count)
        return result

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        """Reconnect by closing and re-logging in."""
        logger.info("fubon_reconnect", reason=reason, force=force)
        try:
            self.close()
            self.login()
            return True
        except Exception:
            logger.exception("fubon_reconnect_failed")
            return False

    def close(self, logout: bool = False) -> None:
        """Mark session as closed."""
        self._logged_in = False

    def shutdown(self, logout: bool = False) -> None:
        """Full teardown — release SDK references."""
        self.close(logout=logout)
        self._sdk = None
        self._account = None
