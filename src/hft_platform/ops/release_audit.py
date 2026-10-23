"""Operator releases of a stop: the token that names the stop, and the audit line.

A release must name the exact stop it clears. Without that, ``hft risk resume``
run from a terminal opened an hour ago clears whatever is latched now -- a new
halt written since (by Telegram ``/stop``, or by the engine's own startup gate)
included. The token is derived from the latch record itself, so it changes
whenever the record is rewritten, and the operator can only quote it after
reading the current record (``hft risk status``, ``hft risk latches``,
``hft ops autonomy-status``).

Every release appends one line to an audit file before the stop is removed, so
a removal never happens without a record of who asked and why.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from hft_platform.core import timebase

UNREADABLE_TOKEN = "unreadable"


def token_for(record: Any) -> str:
    """Short, stable token for one latch record. Unreadable records share one token."""
    if record is None or record == UNREADABLE_TOKEN:
        return UNREADABLE_TOKEN
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def check_release(*, current_token: str, given_token: str, reason: str) -> None:
    """Raise ValueError unless the release names the current stop and gives a reason."""
    if not reason.strip():
        raise ValueError("a release needs a reason")
    if given_token != current_token:
        raise ValueError(
            f"token {given_token!r} does not match the current latch ({current_token!r}); "
            "it changed since you read it. Read its status again before releasing."
        )


def append(audit_path: Path, *, event: str, actor: str, reason: str, removed: Any, **fields: Any) -> None:
    """Append one fsynced JSON line. Blocking IO."""
    entry = {
        "event": event,
        "actor": actor,
        "reason": reason,
        "timestamp_ns": timebase.now_ns(),
        "removed": removed,
        **fields,
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
