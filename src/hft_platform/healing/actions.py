"""ActionRegistry — concrete repair action callables for the healing framework."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import structlog

logger = structlog.get_logger("healing.actions")

ActionFn = Callable[..., Awaitable[None]]


class ActionRegistry:
    __slots__ = ("_actions",)

    def __init__(self) -> None:
        self._actions: dict[str, ActionFn] = {}

    def register(self, name: str, fn: ActionFn) -> None:
        self._actions[name] = fn

    def get(self, name: str) -> ActionFn | None:
        return self._actions.get(name)

    def list_actions(self) -> list[str]:
        return list(self._actions.keys())

    def register_builtins(self) -> None:
        self.register("wait", _action_wait)
        self.register("log_warn", _action_log_warn)


async def _action_wait(*, duration_s: float = 1.0, **kwargs: Any) -> None:
    logger.info("healing_action.wait", duration_s=duration_s)
    await asyncio.sleep(duration_s)


async def _action_log_warn(*, message: str = "healing action triggered", **kwargs: Any) -> None:
    logger.warning("healing_action.log_warn", message=message)
