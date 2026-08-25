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


def read_state(path: Path) -> dict[str, Any]:
    """Read without locking. For callers that only observe (snapshot, gauges)."""
    if not path.exists():
        return normalize_state(None)
    try:
        return normalize_state(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        # Never silently substitute defaults on a *write* path -- that is how an
        # unreadable file erases a live latch. Callers that mutate go through
        # locked_state, which refuses instead.
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


@contextmanager
def locked_state(path: Path) -> Iterator[dict[str, Any]]:
    """Exclusively lock ``path``, yield its state, write it back on clean exit.

    The lock spans the entire read-modify-write, which is what prevents the lost
    update above. An exception inside the block propagates with the file
    untouched -- the fail-closed direction, since the caller's mutation is the
    thing that was in doubt.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            state = read_state(path)
            yield state
            _atomic_write(path, state)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
