"""CE2-05: IdempotencyStore — LRU sliding window dedup for OrderIntents.

Guarantees:
- check_or_reserve(): O(1) — returns cached decision on duplicate keys.
- commit(): records approved/rejected decision.
- Window evicts oldest entry when full (LRU via OrderedDict).
- Persist/load from disk for crash-recovery.
"""

from __future__ import annotations

import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from structlog import get_logger

logger = get_logger("gateway.dedup")

try:
    import orjson

    def _dumps(obj: object) -> bytes:
        return orjson.dumps(obj)

    def _loads(data: bytes | str) -> object:
        return orjson.loads(data)

except ImportError:
    import json

    def _dumps(obj: object) -> bytes:
        return json.dumps(obj).encode()

    def _loads(data: bytes | str) -> object:
        return json.loads(data)


@dataclass(slots=True)
class IdempotencyRecord:
    """Stored decision for a given idempotency_key."""

    key: str
    approved: Optional[bool] = None  # None = reserved (in-flight)
    reason_code: str = ""
    cmd_id: int = 0


class IdempotencyStore:
    """LRU-bounded idempotency window with optional disk persistence.

    Env vars:
        HFT_DEDUP_WINDOW_SIZE:     max keys kept (default 10000)
        HFT_DEDUP_PERSIST_ENABLED: persist on commit (default 1)
        HFT_DEDUP_PERSIST_PATH:    JSONL file path (default .state/dedup_window.jsonl)
    """

    def __init__(
        self,
        window_size: int | None = None,
        persist_enabled: bool | None = None,
        persist_path: str | None = None,
    ) -> None:
        self._window_size = window_size or int(os.getenv("HFT_DEDUP_WINDOW_SIZE", "10000"))
        _pe = (
            persist_enabled
            if persist_enabled is not None
            else (os.getenv("HFT_DEDUP_PERSIST_ENABLED", "1").lower() not in {"0", "false", "no", "off"})
        )
        self._persist_enabled = _pe
        self._persist_path: str = (
            persist_path
            if persist_path is not None
            else os.getenv("HFT_DEDUP_PERSIST_PATH", ".state/dedup_window.jsonl")
        )
        self._records: OrderedDict[str, IdempotencyRecord] = OrderedDict()

    # ── Public API ────────────────────────────────────────────────────────

    def check_or_reserve(self, key: str) -> Optional[IdempotencyRecord]:
        """Check for existing record; reserve slot if new.

        Returns:
            Existing IdempotencyRecord if key was seen before (hit).
            None if key is new (miss) — slot is now reserved.
        """
        if not key:
            return None

        if key in self._records:
            # LRU: move to end
            self._records.move_to_end(key)
            return self._records[key]

        # New key: reserve slot
        rec = IdempotencyRecord(key=key)
        self._records[key] = rec
        self._records.move_to_end(key)

        # Evict oldest if over window
        if len(self._records) > self._window_size:
            self._records.popitem(last=False)

        return None

    def check_or_reserve_typed(self, key: str) -> Optional[IdempotencyRecord]:
        """Typed fast-path alias (string key already extracted upstream)."""
        return self.check_or_reserve(key)

    def commit(
        self,
        key: str,
        approved: bool,
        reason_code: str,
        cmd_id: int,
    ) -> None:
        """Record final decision for a reserved key."""
        if not key:
            return
        if key in self._records:
            rec = self._records[key]
            rec.approved = approved
            rec.reason_code = reason_code
            rec.cmd_id = cmd_id
        else:
            # Commit without prior reserve (tolerated)
            self._records[key] = IdempotencyRecord(key=key, approved=approved, reason_code=reason_code, cmd_id=cmd_id)

    def commit_typed(
        self,
        key: str,
        approved: bool,
        reason_code: str,
        cmd_id: int,
    ) -> None:
        """Typed fast-path alias for consistency with gateway typed path."""
        self.commit(key, approved, reason_code, cmd_id)

    def persist(self) -> None:
        """Write current window to disk atomically (temp+fsync+rename)."""
        if not self._persist_enabled:
            return
        try:
            persist_dir = os.path.dirname(self._persist_path) or "."
            os.makedirs(persist_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=persist_dir)
            try:
                with os.fdopen(fd, "wb") as f:
                    for rec in self._records.values():
                        row = {
                            "key": rec.key,
                            "approved": rec.approved,
                            "reason_code": rec.reason_code,
                            "cmd_id": rec.cmd_id,
                        }
                        f.write(_dumps(row) + b"\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.rename(tmp_path, self._persist_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        except Exception as exc:
            logger.warning("IdempotencyStore persist failed", error=str(exc))

    def load(self) -> None:
        """Load window from disk on startup."""
        if not self._persist_enabled or not os.path.exists(self._persist_path):
            return
        try:
            loaded = 0
            with open(self._persist_path, "rb") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = _loads(raw)
                        if not isinstance(obj, dict):
                            continue
                        rec = IdempotencyRecord(
                            key=str(obj.get("key", "")),
                            approved=obj.get("approved"),
                            reason_code=str(obj.get("reason_code", "")),
                            cmd_id=int(obj.get("cmd_id", 0)),
                        )
                        if rec.key:
                            self._records[rec.key] = rec
                            loaded += 1
                    except Exception:
                        continue
            logger.info("IdempotencyStore loaded", count=loaded)
        except Exception as exc:
            logger.warning("IdempotencyStore load failed", error=str(exc))

    def size(self) -> int:
        return len(self._records)
