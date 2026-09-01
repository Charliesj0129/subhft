"""Serialized access to ``runtime_state.json``.

Two processes write this file: the engine (``AutonomyEvidenceWriter``) and the
operator CLI (``ManualRearmGate``). Both perform a read-modify-write of the
*whole* document, so making each individual write atomic is not enough --
atomicity stops a torn file, it does not stop a lost update:

    CLI reads {platform: false}
    engine writes {platform: true, reason: clickhouse_unhealthy}
    CLI writes back the document it read  ->  platform latch erased

That interleaving was reproduced deterministically on 2026-08-25. It matters
because startup restores platform reduce-only only from this persisted flag, so
a lost latch means a later restart boots NORMAL with no operator re-arm.

The whole transaction therefore has to be serialized, not just the final
rename. ``locked_state`` holds an exclusive ``flock`` across read, mutate, and
replace. The lock lives in a sidecar file so the state file itself can still be
replaced atomically underneath it.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DEFAULT_STATE: dict[str, Any] = {
    "platform": {"manual_rearm_required": False, "reason": None},
    "strategies": {},
}


def normalize_state(raw: Any) -> dict[str, Any]:
    """Coerce a parsed document into the expected shape without losing data."""
    state: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    platform = state.get("platform")
    if not isinstance(platform, dict):
        platform = {"manual_rearm_required": False, "reason": None}
        state["platform"] = platform
    platform.setdefault("manual_rearm_required", False)
    platform.setdefault("reason", None)
    strategies = state.get("strategies")
    if not isinstance(strategies, dict):
        state["strategies"] = {}
    return state


class RuntimeStateUnreadable(RuntimeError):
    """The state file exists but could not be read or parsed."""


def read_state_strict(path: Path) -> dict[str, Any]:
    """Read, distinguishing "missing" from "unreadable".

    A missing file is a legitimate cold start and yields defaults. Anything else
    -- malformed JSON, EACCES, a truncated write -- raises. Mutating callers MUST
    use this: substituting defaults there is precisely how an unreadable file
    erases a persisted platform HALT latch, letting a later restart come up
    NORMAL with no operator re-arm.
    """
    if not path.exists():
        return normalize_state(None)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeStateUnreadable(f"{path} exists but could not be read: {exc}") from exc
    return normalize_state(raw)


def read_state(path: Path) -> dict[str, Any]:
    """Tolerant read for observers only (snapshots, gauges, status output).

    Never use on a path that writes the result back -- see ``read_state_strict``.
    """
    try:
        return read_state_strict(path)
    except RuntimeStateUnreadable:
        return normalize_state(None)


def _atomic_write(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Writer-unique temp name: a shared `.tmp` means whichever process renames
    # second either moves the other's payload or fails with FileNotFoundError.
    tmp_path = path.with_suffix(f"{path.suffix}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


class RuntimeStateLockTimeout(TimeoutError):
    """The state lock could not be acquired within the deadline."""


#: Default acquisition deadline. A blocking ``LOCK_EX`` has no deadline at all:
#: a stopped CLI still holding the lock, or a stalled volume, would block the
#: caller forever -- and ``StrategyHealthGovernor.quarantine`` reaches this from
#: the event loop, so that would freeze market, risk and order processing until
#: the loop watchdog hard-exits the engine. Bounded and fail-closed instead.
DEFAULT_LOCK_TIMEOUT_S = 2.0


@contextmanager
def locked_state(path: Path, *, timeout_s: float = DEFAULT_LOCK_TIMEOUT_S) -> Iterator[dict[str, Any]]:
    """Exclusively lock ``path``, yield its state, write it back on clean exit.

    The lock spans the entire read-modify-write, which is what prevents a lost
    update: making each replacement atomic stops a torn file, it does not stop
    one writer clobbering another's section.

    Acquisition is bounded (``RuntimeStateLockTimeout``) and the read is strict
    (``RuntimeStateUnreadable``). Both failures leave the file byte-for-byte
    untouched, and an exception raised inside the block does too -- the caller's
    mutation is the thing in doubt, so publishing nothing is fail-closed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeStateLockTimeout(f"could not acquire {lock_path} within {timeout_s}s") from None
                time.sleep(0.01)
        try:
            state = read_state_strict(path)
            yield state
            _atomic_write(path, state)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
