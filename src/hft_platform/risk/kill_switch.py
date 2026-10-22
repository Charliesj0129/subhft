"""File-backed emergency halt latch.

The engine container runs under ``restart: always``, so an exit code latches
nothing: a clean exit and a crash are both restarted into a fresh process with
a fresh in-memory StormGuard. Only state that outlives the process can refuse
the next boot, and this file is it.

Written by an operator (``hft risk halt``) or by a startup safety gate that
refuses to trade; read every supervise tick by
:meth:`hft_platform.services.system.HFTSystem._supervise`, by ``hft health``
and by ``hft golive``. Cleared only by ``hft risk resume`` -- nothing in the
engine removes it, because a latch a process can clear by restarting is not a
latch.

Durability: the default path is under ``.state/``, the engine's bind mount on
the production host, so the latch survives container recreation as well as
stop/start. It used to be ``.runtime/kill_switch``, which is the container's
writable layer there; that path is still honoured as a legacy location so a
latch written before the move is not silently dropped. ``HFT_KILL_SWITCH_PATH``
overrides both. The write is atomic and fsynced: a torn or lost write of a
stop is a release nobody authorized.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hft_platform.core import timebase
from hft_platform.ops.runtime_state_store import atomic_write_json

DEFAULT_PATH = ".state/kill_switch"
LEGACY_PATHS: tuple[str, ...] = (".runtime/kill_switch",)
PATH_ENV = "HFT_KILL_SWITCH_PATH"


def kill_switch_path() -> str:
    """Resolve the latch path from the environment."""
    return os.getenv(PATH_ENV, DEFAULT_PATH)


def _candidates(path: str | None) -> tuple[str, ...]:
    if path is not None:
        return (path,)
    if os.getenv(PATH_ENV):
        return (kill_switch_path(),)
    return (DEFAULT_PATH, *LEGACY_PATHS)


def active_path(path: str | None = None) -> str | None:
    """The first latch file present, or None. With no ``path``, legacy locations count too."""
    for candidate in _candidates(path):
        if os.path.exists(candidate):
            return candidate
    return None


def is_active(path: str | None = None) -> bool:
    """True when a latch file exists. Presence is the signal; content is detail."""
    return active_path(path) is not None


def activate(reason: str, *, actor: str, path: str | None = None) -> str:
    """Write the latch durably and return the path written.

    An existing latch is overwritten rather than preserved: the newest reason is
    the one the operator has yet to act on, and keeping an older, already
    understood reason would mask it.
    """
    target = path or kill_switch_path()
    payload: dict[str, Any] = {"reason": reason, "timestamp_ns": timebase.now_ns(), "actor": actor}
    atomic_write_json(Path(target), payload)
    return target


def deactivate(path: str | None = None) -> bool:
    """Remove the latch from every location it may be in. False when there was nothing to remove."""
    removed = False
    for candidate in _candidates(path):
        if os.path.exists(candidate):
            os.remove(candidate)
            removed = True
    return removed


def read_payload(path: str | None = None) -> dict[str, Any]:
    """Read the latch record. Raises on a missing or malformed file.

    Callers decide what an unreadable-but-present latch means; the engine treats
    it as active regardless, because presence is the signal.
    """
    target = active_path(path) or path or kill_switch_path()
    with open(target, "r") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def read_reason(path: str | None = None) -> str:
    """Reason recorded in the latch. Runs in an executor thread on the engine loop."""
    return str(read_payload(path).get("reason", "unknown"))
