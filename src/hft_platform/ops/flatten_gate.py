"""FlattenGate — file-based IPC for emergency position flattening.

CLI writes a ``flatten_request.json``; the running engine polls, claims,
executes, and writes back the result.  Follows the ManualRearmGate atomic-
write pattern (write to .tmp, rename).
"""

from __future__ import annotations

import enum
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger("flatten_gate")

DEFAULT_FLATTEN_REQUEST_PATH = Path(
    "outputs/production_rollout/autonomy/flatten_request.json"
)


class FlattenStatus(enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(slots=True)
class FlattenRequest:
    scope: str  # "all", "strategy", "track"
    scope_id: str | None
    deadline_s: int
    status: FlattenStatus
    initiated_ns: int
    fully_closed: int = 0
    partially_closed: int = 0
    failed: int = 0
    failed_symbols: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "scope_id": self.scope_id,
            "deadline_s": self.deadline_s,
            "status": self.status.value,
            "initiated_ns": self.initiated_ns,
            "fully_closed": self.fully_closed,
            "partially_closed": self.partially_closed,
            "failed": self.failed,
            "failed_symbols": list(self.failed_symbols),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlattenRequest:
        return cls(
            scope=str(data.get("scope", "all")),
            scope_id=data.get("scope_id"),
            deadline_s=int(data.get("deadline_s", 120)),
            status=FlattenStatus(data.get("status", "PENDING")),
            initiated_ns=int(data.get("initiated_ns", 0)),
            fully_closed=int(data.get("fully_closed", 0)),
            partially_closed=int(data.get("partially_closed", 0)),
            failed=int(data.get("failed", 0)),
            failed_symbols=list(data.get("failed_symbols", [])),
            error=data.get("error"),
        )


class FlattenGate:
    """File-based IPC gate for flatten requests.

    State machine: PENDING -> PROCESSING -> COMPLETED | FAILED
    """

    __slots__ = ("_path",)

    def __init__(self, *, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_FLATTEN_REQUEST_PATH

    @property
    def path(self) -> Path:
        return self._path

    def submit(
        self,
        scope: str = "all",
        scope_id: str | None = None,
        deadline_s: int = 120,
    ) -> FlattenRequest:
        """Write a new PENDING flatten request."""
        req = FlattenRequest(
            scope=scope,
            scope_id=scope_id,
            deadline_s=deadline_s,
            status=FlattenStatus.PENDING,
            initiated_ns=time.monotonic_ns(),
        )
        self._write(req)
        logger.info(
            "flatten_request_submitted",
            scope=scope,
            scope_id=scope_id,
            deadline_s=deadline_s,
        )
        return req

    def read_request(self) -> FlattenRequest | None:
        """Read the current flatten request.  Returns None if no file."""
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(raw, dict):
            return None
        return FlattenRequest.from_dict(raw)

    def claim(self) -> FlattenRequest | None:
        """Transition PENDING -> PROCESSING.  Returns None if not PENDING."""
        req = self.read_request()
        if req is None or req.status != FlattenStatus.PENDING:
            return None
        req.status = FlattenStatus.PROCESSING
        self._write(req)
        logger.info("flatten_request_claimed", scope=req.scope)
        return req

    def complete(
        self,
        fully_closed: int,
        partially_closed: int,
        failed: int,
        failed_symbols: list[str] | None = None,
    ) -> None:
        """Mark the request as COMPLETED with result data."""
        req = self.read_request()
        if req is None:
            return
        req.status = FlattenStatus.COMPLETED
        req.fully_closed = fully_closed
        req.partially_closed = partially_closed
        req.failed = failed
        req.failed_symbols = list(failed_symbols) if failed_symbols else []
        self._write(req)
        logger.info(
            "flatten_request_completed",
            fully_closed=fully_closed,
            partially_closed=partially_closed,
            failed=failed,
        )

    def fail(self, error: str) -> None:
        """Mark the request as FAILED with an error message."""
        req = self.read_request()
        if req is None:
            return
        req.status = FlattenStatus.FAILED
        req.error = error
        self._write(req)
        logger.error("flatten_request_failed", error=error)

    def clear(self) -> None:
        """Remove the request file."""
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass

    def _write(self, req: FlattenRequest) -> None:
        """Atomic write via tmp + rename."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(req.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self._path)
