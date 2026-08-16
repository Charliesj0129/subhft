"""Process-local registry for the shared platform-degrade controller."""

from __future__ import annotations

from threading import Lock
from typing import Any, Callable, Protocol, TypeVar, cast


class SharedPlatformDegradeController(Protocol):
    """Controller capabilities required by the process-local registry."""

    metrics: Any | None

    def _sync_metrics(self) -> None: ...

    def force_clear(self, *, reason: str = "manual_rearm") -> object | None: ...


_ControllerT = TypeVar("_ControllerT", bound=SharedPlatformDegradeController)
_shared_controller: SharedPlatformDegradeController | None = None
_shared_controller_lock = Lock()


def get_or_create_shared_controller(
    factory: Callable[[], _ControllerT],
    *,
    metrics: Any | None,
) -> _ControllerT:
    """Return the singleton, creating and restoring it while holding its lock."""
    global _shared_controller
    with _shared_controller_lock:
        created = False
        if _shared_controller is None:
            _shared_controller = factory()
            created = True

        controller = cast(_ControllerT, _shared_controller)
        if not created and metrics is not None and controller.metrics is None:
            controller.metrics = metrics
            controller._sync_metrics()
        return controller


def try_force_clear_shared_controller(*, reason: str) -> bool:
    """Force-clear the initialized singleton without holding its registry lock."""
    with _shared_controller_lock:
        controller = _shared_controller
    if controller is None:
        return False
    controller.force_clear(reason=reason)
    return True


def reset_shared_controller() -> None:
    """Discard the process-local singleton."""
    global _shared_controller
    with _shared_controller_lock:
        _shared_controller = None
