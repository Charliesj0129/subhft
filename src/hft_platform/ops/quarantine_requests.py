"""Operator-published requests to STOP a strategy.

The re-arm channel (:mod:`hft_platform.ops.rearm_requests`) has existed for
months; its inverse did not. On 2026-09-03 that gap was measured: containing
``R47_MAKER_TMF`` by setting ``enabled: false`` in
``config/live/strategies.yaml`` put the engine into a crash loop, because
``config/loader.py:_assert_strategy_enabled`` refuses to start when the
loop-bound strategy is disabled -- correctly, it is a fail-fast on a binding
that would otherwise fail silently. With no runtime lever either, the only way
to stop the platform's only strategy was to stop the engine.

``StrategyHealthGovernor.quarantine()`` and ``quarantine_async()`` already
existed with no operator-reachable caller. This module is the channel that
reaches them, and it deliberately mirrors ``rearm_requests``: one file per
request, created with ``O_EXCL`` and published with ``os.link`` so a duplicate
id fails loudly instead of overwriting, consumed by ``unlink``.

Two things differ from re-arm, both because the directions are not symmetric:

* **No ``quarantine_token``.** A re-arm authorizes the release of one specific
  latch, so it must name it. A quarantine *creates* the latch -- there is
  nothing yet to name.
* **A TTL instead.** With no token there is nothing to make a forgotten request
  stale, and an authorization to stop trading that surfaces weeks later would
  be a surprise, not a safety win. Requests older than ``max_age_s`` are
  retired unapplied and logged. The default is generous (24 h): stopping is the
  fail-closed direction, so the TTL exists to bound surprise, not to race the
  operator.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, NamedTuple

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("quarantine_requests")

REQUEST_SUFFIX = ".json"

#: Requests older than this are retired without being applied.
DEFAULT_MAX_AGE_S = 86_400.0


class QuarantineRequest(NamedTuple):
    path: Path
    request_id: str
    strategy_id: str
    reason: str
    requested_at_ns: int


def request_dir(base_dir: Path) -> Path:
    return base_dir / "quarantine_requests"


def publish(base_dir: Path, *, strategy_id: str, reason: str, request_id: str) -> Path:
    """Write one request. Fails if the id already exists rather than overwriting."""
    directory = request_dir(base_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{request_id}{REQUEST_SUFFIX}"
    payload = {
        "request_id": request_id,
        "strategy_id": strategy_id,
        "reason": reason,
        "requested_at_ns": timebase.now_ns(),
    }
    tmp_path = directory / f".{request_id}.{os.getpid()}.tmp"
    fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.link(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return path


def pending(base_dir: Path) -> list[QuarantineRequest]:
    """List well-formed requests, oldest filename first.

    A malformed request is skipped and left in place for an operator to see --
    deleting it would hide the problem, and it cannot be acted on either way.
    """
    directory = request_dir(base_dir)
    if not directory.is_dir():
        return []
    found: list[QuarantineRequest] = []
    for entry in sorted(directory.glob(f"*{REQUEST_SUFFIX}")):
        try:
            payload: Any = json.loads(entry.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - unreadable request is operator-visible
            logger.warning("quarantine_request_unreadable", path=str(entry), error=str(exc))
            continue
        if not isinstance(payload, dict):
            logger.warning("quarantine_request_malformed", path=str(entry))
            continue
        request_id = payload.get("request_id")
        strategy_id = payload.get("strategy_id")
        if not (isinstance(request_id, str) and request_id):
            continue
        if not (isinstance(strategy_id, str) and strategy_id):
            continue
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason:
            reason = "operator_request"
        try:
            requested_at_ns = int(payload.get("requested_at_ns") or 0)
        except (TypeError, ValueError):
            requested_at_ns = 0
        found.append(QuarantineRequest(entry, request_id, strategy_id, reason, requested_at_ns))
    return found


def is_expired(request: QuarantineRequest, *, max_age_s: float = DEFAULT_MAX_AGE_S) -> bool:
    """Whether ``request`` is too old to apply.

    A request with no usable timestamp is treated as expired: the engine cannot
    tell how old it is, and applying an authorization of unknown age is the one
    thing the TTL exists to prevent.
    """
    if request.requested_at_ns <= 0:
        return True
    age_s = (timebase.now_ns() - request.requested_at_ns) / 1e9
    return age_s > max_age_s


def consume(request: QuarantineRequest) -> None:
    """Remove a request. One syscall; idempotent if it is already gone."""
    request.path.unlink(missing_ok=True)
