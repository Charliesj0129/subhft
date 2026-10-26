"""File-backed emergency halt latch.

The engine container runs under ``restart: always``, so an exit code latches
nothing: a clean exit and a crash are both restarted into a fresh process with
a fresh in-memory StormGuard. Only state that outlives the process can refuse
the next boot, and this file is it.

Written by an operator (``hft risk halt`` or Telegram ``/stop``) or by a
startup safety gate that refuses to trade; read every supervise tick by
:meth:`hft_platform.services.system.HFTSystem._supervise`, by ``hft health``
and by ``hft golive``. Cleared only by ``hft risk resume``, which must quote the
current latch's token and a reason and leaves an audit line (:func:`release`) --
nothing in the engine removes it, because a latch a process can clear by
restarting is not a latch.

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
from hft_platform.ops import release_audit
from hft_platform.ops.runtime_state_store import atomic_write_json, exclusive_lock

DEFAULT_PATH = ".state/kill_switch"
LEGACY_PATHS: tuple[str, ...] = (".runtime/kill_switch",)
PATH_ENV = "HFT_KILL_SWITCH_PATH"
AUDIT_FILE = "kill_switch_audit.jsonl"


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
    with exclusive_lock(Path(target)):
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


def _records(path: str | None) -> dict[str, Any]:
    """Every present latch file mapped to its record (or the unreadable marker)."""
    found: dict[str, Any] = {}
    for candidate in _candidates(path):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r") as fh:
                found[candidate] = json.load(fh)
        except (OSError, ValueError):
            found[candidate] = release_audit.UNREADABLE_TOKEN
    return found


def current_token(path: str | None = None) -> str | None:
    """Token naming the latch now in force, or None when there is none.

    With a latch in more than one location, the token covers all of them: a
    release removes every one, so it must have been read with every one present.
    """
    records = _records(path)
    return _token_of(records) if records else None


def _token_of(records: dict[str, Any]) -> str:
    if len(records) == 1:
        return release_audit.token_for(next(iter(records.values())))
    return release_audit.token_for(records)


def release(*, token: str, reason: str, actor: str, path: str | None = None) -> bool:
    """Remove the latch after an audit line. False when there was nothing to remove.

    Raises ValueError when ``token`` is not the current latch's token or the
    reason is empty; nothing is removed then.

    The check and the removal share :func:`activate`'s lock: a stop written in
    between would otherwise be deleted on the authority of the token for the
    one before it. Only the primary location is ever written, so its lock
    covers the legacy files too.
    """
    with exclusive_lock(Path(path or kill_switch_path())):
        records = _records(path)
        if not records:
            return False
        release_audit.check_release(current_token=_token_of(records), given_token=token, reason=reason)
        audit_path = Path(next(iter(records))).with_name(AUDIT_FILE)
        release_audit.append(audit_path, event="kill_switch_release", actor=actor, reason=reason, removed=records)
        for candidate in records:
            os.remove(candidate)
        return True
