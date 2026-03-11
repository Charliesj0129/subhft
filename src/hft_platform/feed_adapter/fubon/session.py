"""Fubon session management stub."""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class FubonSessionRuntime:
    """Session lifecycle management for Fubon TradeAPI.

    All methods raise ``NotImplementedError`` until the Fubon SDK
    integration is implemented.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    def login(self, *args: Any, **kwargs: Any) -> bool:
        raise NotImplementedError("FubonSessionRuntime.login not yet implemented")

    def refresh_token(self) -> bool:
        raise NotImplementedError("FubonSessionRuntime.refresh_token not yet implemented")

    def logout(self) -> None:
        raise NotImplementedError("FubonSessionRuntime.logout not yet implemented")
