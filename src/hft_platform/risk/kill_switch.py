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

Note on durability: the default path lives under ``.runtime/``, which on the
production host is the container's writable layer rather than a bind mount. It
survives stop/start and restart-policy restarts (the only restart shapes the
deploy runbook allows) but not container recreation. Point
``HFT_KILL_SWITCH_PATH`` at a bind-mounted directory to survive that too.
"""

from __future__ import annotations

import json
import os
from typing import Any

from hft_platform.core import timebase

DEFAULT_PATH = ".runtime/kill_switch"
PATH_ENV = "HFT_KILL_SWITCH_PATH"


def kill_switch_path() -> str:
    """Resolve the latch path from the environment."""
    return os.getenv(PATH_ENV, DEFAULT_PATH)


def is_active(path: str | None = None) -> bool:
    """True when the latch file exists. Presence is the signal; content is detail."""
    return os.path.exists(path or kill_switch_path())


def activate(reason: str, *, actor: str, path: str | None = None) -> str:
    """Write the latch and return the path written.

    An existing latch is overwritten rather than preserved: the newest reason is
    the one the operator has yet to act on, and keeping an older, already
    understood reason would mask it.
    """
    target = path or kill_switch_path()
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload: dict[str, Any] = {"reason": reason, "timestamp_ns": timebase.now_ns(), "actor": actor}
    with open(target, "w") as fh:
        json.dump(payload, fh)
    return target


def deactivate(path: str | None = None) -> bool:
    """Remove the latch. Returns False when there was nothing to remove."""
    target = path or kill_switch_path()
    if os.path.exists(target):
        os.remove(target)
        return True
    return False


def read_payload(path: str | None = None) -> dict[str, Any]:
    """Read the latch record. Raises on a missing or malformed file.

    Callers decide what an unreadable-but-present latch means; the engine treats
    it as active regardless, because presence is the signal.
    """
    with open(path or kill_switch_path(), "r") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def read_reason(path: str | None = None) -> str:
    """Reason recorded in the latch. Runs in an executor thread on the engine loop."""
    return str(read_payload(path).get("reason", "unknown"))
