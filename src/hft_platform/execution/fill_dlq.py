"""Dead-letter queue for orphaned fills (WU-03)."""

import os
import tempfile
from collections import deque
from dataclasses import asdict
from typing import Any

from structlog import get_logger

from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("execution.fill_dlq")

_DEFAULT_PERSIST_PATH = ".state/fill_dlq.jsonl"


class OrphanedFillDLQ:
    __slots__ = ("_queue", "_max_size", "_persist_path")

    def __init__(self, max_size: int = 1000, persist_path: str | None = None) -> None:
        self._queue: deque[Any] = deque(maxlen=max_size)
        self._max_size = max_size
        self._persist_path: str = persist_path or os.getenv("HFT_FILL_DLQ_PERSIST_PATH", _DEFAULT_PERSIST_PATH)

    def add(self, fill_event: Any) -> None:
        if len(self._queue) == self._max_size:
            MetricsRegistry.get().fill_dlq_overflow_total.inc()
            logger.error(
                "fill_dlq_overflow",
                symbol=getattr(fill_event, "symbol", ""),
                order_id=getattr(fill_event, "order_id", ""),
                dlq_size=len(self._queue),
                msg="Oldest orphaned fill silently evicted — position drift risk",
            )
        self._queue.append(fill_event)
        logger.warning(
            "Orphaned fill added to DLQ", symbol=getattr(fill_event, "symbol", ""), dlq_size=len(self._queue)
        )

    @property
    def count(self) -> int:
        return len(self._queue)

    def drain(self) -> list[Any]:
        items = list(self._queue)
        self._queue.clear()
        return items

    def retry(self, resolver_fn: Any) -> tuple[list[Any], list[Any]]:
        """Attempt to re-resolve orphaned fills.

        Args:
            resolver_fn: Callable that takes a fill event and returns a strategy_id str.

        Returns:
            (resolved, still_orphaned) — resolved fills have strategy_id set.
        """
        items = list(self._queue)
        self._queue.clear()
        resolved = []
        still_orphaned = []
        for fill in items:
            new_strategy_id = resolver_fn(fill)
            if new_strategy_id and new_strategy_id != "UNKNOWN":
                fill.strategy_id = new_strategy_id
                resolved.append(fill)
            else:
                still_orphaned.append(fill)
        for f in still_orphaned:
            self._queue.append(f)
        return resolved, still_orphaned

    def persist(self) -> None:
        """Persist DLQ to disk atomically (temp+fsync+rename).

        Called during graceful shutdown so orphaned fills survive restart.
        """
        path = self._persist_path
        snapshot = list(self._queue)
        if not snapshot:
            # Remove stale file if queue is empty
            if os.path.exists(path):
                os.unlink(path)
            return
        try:
            import orjson

            persist_dir = os.path.dirname(path) or "."
            os.makedirs(persist_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=persist_dir)
            try:
                with os.fdopen(fd, "wb") as f:
                    for fill in snapshot:
                        try:
                            row = asdict(fill)
                            # Side is IntEnum — orjson handles it natively
                            f.write(orjson.dumps(row) + b"\n")
                        except Exception:
                            continue
                    f.flush()
                    os.fsync(f.fileno())
                os.rename(tmp_path, path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
            logger.info("fill_dlq_persisted", count=len(snapshot), path=path)
        except Exception as exc:
            logger.warning("fill_dlq_persist_failed", error=str(exc), path=path)

    def load(self) -> None:
        """Load DLQ from disk on startup."""
        path = self._persist_path
        if not os.path.exists(path):
            return
        try:
            import orjson

            from hft_platform.contracts.execution import FillEvent
            from hft_platform.contracts.strategy import Side

            loaded = 0
            with open(path, "rb") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = orjson.loads(raw)
                        if not isinstance(obj, dict):
                            continue
                        obj["side"] = Side(obj["side"])
                        fill = FillEvent(**obj)
                        self._queue.append(fill)
                        loaded += 1
                    except Exception:
                        continue
            logger.info("fill_dlq_loaded", count=loaded, path=path)
        except Exception as exc:
            logger.warning("fill_dlq_load_failed", error=str(exc), path=path)


_dlq: OrphanedFillDLQ | None = None


def get_orphaned_fill_dlq() -> OrphanedFillDLQ:
    global _dlq
    if _dlq is None:
        _dlq = OrphanedFillDLQ()
        _dlq.load()
    return _dlq
