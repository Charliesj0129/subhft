"""Write-once re-arm requests, as files rather than fields.

Four review rounds of a shared mutable ``runtime_state.json`` produced, in
order: a lost update between two processes, a TOCTOU on the acknowledgement, an
unbounded lock reachable from the event loop, a worker-thread race that let an
authorization for one quarantine clear a newer one, and a timeout that could not
stop the worker it timed out. Every fix was real and every fix opened something
else, which is the signal that the channel was wrong -- not that it needed
another guard.

The channel here removes the shared mutable document instead of protecting it:

* **One file per request, written once, never modified.** Created with
  ``O_EXCL``, so there is no read-modify-write, therefore no lost update, no
  compare-and-swap, and nothing to lock.
* **Consumed by ``unlink``.** A single atomic syscall. Replay is impossible
  because the request is gone, not because a watermark says so.
* **The engine decides in memory.** The gate is
  ``StrategyHealthGovernor._quarantined``; matching a request's token against it
  is dict work measured in microseconds, so the decision needs no thread, no
  deadline, and no ordering against a disk write.

That last point rests on a property worth stating plainly: **a strategy
quarantine does not survive a restart.** Only the platform scope has
``_restore_manual_rearm_state``; there is no strategy equivalent. So durability
was never a requirement of this path, and every mechanism built to guarantee it
was protecting state that a restart discards anyway.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, NamedTuple

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("rearm_requests")

REQUEST_SUFFIX = ".json"


class RearmRequest(NamedTuple):
    path: Path
    request_id: str
    strategy_id: str
    quarantine_token: str


def request_dir(base_dir: Path) -> Path:
    return base_dir / "rearm_requests"


def publish(base_dir: Path, *, strategy_id: str, quarantine_token: str, request_id: str) -> Path:
    """Write one request. Fails if the id already exists rather than overwriting."""
    directory = request_dir(base_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{request_id}{REQUEST_SUFFIX}"
    payload = {
        "request_id": request_id,
        "strategy_id": strategy_id,
        "quarantine_token": quarantine_token,
        "requested_at_ns": timebase.now_ns(),
    }
    # Write to a hidden temp name first so a partial file is never visible under
    # a name the scanner looks at, then publish with os.link -- which fails
    # atomically with FileExistsError if the id is already taken. `replace`
    # would silently overwrite, and a duplicate id means something upstream is
    # wrong, not something to paper over.
    tmp_path = directory / f".{request_id}.{os.getpid()}.tmp"
    fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.link(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return path


def pending(base_dir: Path) -> list[RearmRequest]:
    """List well-formed requests. Cheap: one ``scandir`` when the dir is empty.

    A malformed request is skipped and left in place for an operator to see --
    deleting it would hide the problem, and it cannot be acted on either way.
    """
    directory = request_dir(base_dir)
    if not directory.is_dir():
        return []
    found: list[RearmRequest] = []
    for entry in sorted(directory.glob(f"*{REQUEST_SUFFIX}")):
        try:
            payload: Any = json.loads(entry.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("rearm_request_unreadable", path=str(entry), error=str(exc))
            continue
        if not isinstance(payload, dict):
            logger.warning("rearm_request_malformed", path=str(entry))
            continue
        request_id = payload.get("request_id")
        strategy_id = payload.get("strategy_id")
        token = payload.get("quarantine_token")
        if not (isinstance(request_id, str) and request_id):
            continue
        if not (isinstance(strategy_id, str) and strategy_id):
            continue
        if not (isinstance(token, str) and token):
            # No token means the engine cannot tell which quarantine this
            # targets. Fail closed: never treat it as authorizing anything.
            logger.warning("rearm_request_without_token", path=str(entry), strategy_id=strategy_id)
            continue
        found.append(RearmRequest(entry, request_id, strategy_id, token))
    return found


def consume(request: RearmRequest) -> None:
    """Remove a request. One syscall; idempotent if it is already gone."""
    request.path.unlink(missing_ok=True)
