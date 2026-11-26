"""Fubon session management.

Delegates to ``FubonSessionRuntime`` from ``session_runtime.py`` when
available; otherwise falls back to stub behaviour.
"""

from __future__ import annotations

from typing import Any

import structlog

try:
    from hft_platform.feed_adapter.fubon.session_runtime import (
        FubonSessionRuntime as _Impl,
    )

    _HAS_IMPL = True
except ImportError:
    _HAS_IMPL = False

logger = structlog.get_logger(__name__)


class FubonSessionRuntime:
    """Session lifecycle management for Fubon TradeAPI.

    When the real ``session_runtime`` module is available, all methods
    delegate to it.  Otherwise, ``NotImplementedError`` is raised to
    signal that the concrete implementation has not been installed.

    *config* is forwarded to the implementation; ``config["sdk_url"]`` is the
    endpoint the SDK was built with, which login checks outside live mode.
    """

    __slots__ = ("_client", "_impl")

    def __init__(self, client: Any, config: dict[str, Any] | None = None) -> None:
        self._client = client
        self._impl: Any = _Impl(client, config) if _HAS_IMPL else None

    @property
    def is_logged_in(self) -> bool:
        if self._impl is not None:
            return bool(self._impl.is_logged_in)
        return False

    @property
    def account(self) -> Any:
        if self._impl is not None:
            return self._impl.account
        return None

    def login(self, *args: Any, **kwargs: Any) -> bool:
        if self._impl is not None:
            return self._impl.login(*args, **kwargs)
        raise NotImplementedError("FubonSessionRuntime.login not yet implemented")

    def login_with_retry(self, *args: Any, **kwargs: Any) -> bool:
        if self._impl is not None:
            return self._impl.login_with_retry(*args, **kwargs)
        raise NotImplementedError("FubonSessionRuntime.login_with_retry not yet implemented")

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        if self._impl is not None:
            return self._impl.reconnect(reason=reason, force=force)
        raise NotImplementedError("FubonSessionRuntime.reconnect not yet implemented")

    def refresh_token(self) -> bool:
        if self._impl is not None:
            return self._impl.refresh_token()
        raise NotImplementedError("FubonSessionRuntime.refresh_token not yet implemented")

    def logout(self) -> None:
        if self._impl is not None:
            return self._impl.logout()
        raise NotImplementedError("FubonSessionRuntime.logout not yet implemented")
