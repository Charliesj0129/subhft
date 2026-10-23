"""Durable manual latches: stops that only an operator can release.

Policy (2026-10-23): four conditions require an operator to resume trading --
data loss, a reconciliation mismatch, an operator kill and the daily loss
limit. Everything else recovers on its own. Data loss is the platform
reduce-only latch in ``runtime_state.json`` and the operator kill is
``risk/kill_switch.py``; this module holds the other two.

Same shape as the kill switch, deliberately: one file per latch, and presence
is the signal. The engine writes a latch and never removes one; ``hft risk
rearm`` removes it, quoting the latch's token (``hft risk latches``) and a
reason, and appends an audit line. A restart therefore cannot
release a latch, which is the property the in-memory holds lacked -- on
2026-10-22 a deploy restart cleared a daily-loss HALT in place.

The default directory is ``.state/manual_latches`` because ``.state`` is the
engine's bind mount on the production host; ``.runtime`` and ``outputs`` are
the container's writable layer there and do not survive a recreate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hft_platform.core import timebase
from hft_platform.ops import release_audit
from hft_platform.ops.runtime_state_store import atomic_write_json

DEFAULT_DIR = ".state/manual_latches"
DIR_ENV = "HFT_MANUAL_LATCH_DIR"

DAILY_LOSS = "daily_loss"
RECONCILIATION_MISMATCH = "reconciliation_mismatch"
LATCH_NAMES: frozenset[str] = frozenset({DAILY_LOSS, RECONCILIATION_MISMATCH})

AUDIT_FILE = "audit.jsonl"
_SUFFIX = ".json"


def latch_dir() -> Path:
    """Resolve the latch directory from the environment."""
    return Path(os.getenv(DIR_ENV, DEFAULT_DIR))


def _latch_path(name: str, directory: Path | None) -> Path:
    if name not in LATCH_NAMES:
        raise ValueError(f"unknown manual latch {name!r}; expected one of {sorted(LATCH_NAMES)}")
    return (directory or latch_dir()) / f"{name}{_SUFFIX}"


def activate(name: str, reason: str, *, actor: str, directory: Path | None = None) -> bool:
    """Write the latch durably. Returns False when it was already present.

    An existing latch is kept, not overwritten: it records the first time the
    condition fired, which is what the operator has to investigate. Blocking IO
    (two fsyncs); callers on the event loop run it in a thread.
    """
    target = _latch_path(name, directory)
    if target.exists():
        return False
    payload: dict[str, Any] = {
        "latch": name,
        "reason": reason,
        "actor": actor,
        "timestamp_ns": timebase.now_ns(),
    }
    atomic_write_json(target, payload)
    return True


def active(directory: Path | None = None) -> dict[str, str]:
    """Map each present latch to its recorded reason.

    A file that cannot be read still counts: presence is the signal, and an
    unreadable latch resolved as absent would be a release nobody authorized.
    """
    base = directory or latch_dir()
    found: dict[str, str] = {}
    for name in sorted(LATCH_NAMES):
        path = base / f"{name}{_SUFFIX}"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            found[name] = str(data.get("reason", "unknown")) if isinstance(data, dict) else "unreadable"
        except (OSError, ValueError):
            found[name] = "unreadable"
    return found


def _record(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return release_audit.UNREADABLE_TOKEN


def tokens(directory: Path | None = None) -> dict[str, str]:
    """Map each present latch to the token a release of it must quote."""
    base = directory or latch_dir()
    return {
        name: release_audit.token_for(_record(base / f"{name}{_SUFFIX}"))
        for name in sorted(LATCH_NAMES)
        if (base / f"{name}{_SUFFIX}").exists()
    }


def release(name: str, *, token: str, reason: str, actor: str, directory: Path | None = None) -> bool:
    """Remove the latch and append an audit line. Returns False if it was absent.

    Raises ValueError, removing nothing, when ``token`` is not the latch's
    current token or the reason is empty. The audit line is written first, so a
    removal never happens without a record of who asked for it and why.
    """
    if not reason.strip():
        raise ValueError("a release needs a reason")
    target = _latch_path(name, directory)
    if not target.exists():
        return False
    removed = _record(target)
    release_audit.check_release(current_token=release_audit.token_for(removed), given_token=token, reason=reason)
    release_audit.append(
        target.parent / AUDIT_FILE, event="release", actor=actor, reason=reason, removed=removed, latch=name
    )
    target.unlink()
    return True
