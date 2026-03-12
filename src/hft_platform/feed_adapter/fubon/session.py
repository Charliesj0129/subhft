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
    """

    __slots__ = ("_client", "_impl")

    def __init__(self, client: Any) -> None:
        self._client = client
        self._impl: Any = _Impl(client) if _HAS_IMPL else None

    def login(self, *args: Any, **kwargs: Any) -> bool:
        if self._impl is not None:
            return self._impl.login(*args, **kwargs)
        raise NotImplementedError("FubonSessionRuntime.login not yet implemented")

    def refresh_token(self) -> bool:
        if self._impl is not None:
            return self._impl.refresh_token()
        raise NotImplementedError("FubonSessionRuntime.refresh_token not yet implemented")

    def logout(self) -> None:
        if self._impl is not None:
            return self._impl.logout()
        raise NotImplementedError("FubonSessionRuntime.logout not yet implemented")
