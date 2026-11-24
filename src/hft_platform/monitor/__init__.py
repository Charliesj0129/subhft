"""Signal Monitor TUI package.

The exports load on first access (PEP 562). `rich`, which the TUI needs, is an
optional extra (`monitor`), and the free-threaded runtime image does not ship it.
A leaf import such as `hft_platform.monitor._redis_publish` (the engine's live
publisher) must not pull the TUI in through this file.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hft_platform.monitor._engine import MonitorEngine
    from hft_platform.monitor._redis_publish import MonitorLivePublisher
    from hft_platform.monitor._tui import run_monitor

__all__ = ["MonitorEngine", "MonitorLivePublisher", "run_monitor"]

_LAZY_EXPORTS: dict[str, str] = {
    "MonitorEngine": "hft_platform.monitor._engine",
    "MonitorLivePublisher": "hft_platform.monitor._redis_publish",
    "run_monitor": "hft_platform.monitor._tui",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
