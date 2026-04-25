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
    __slots__ = ("_queue", "_max_size", "_persist_path", "_overflow_evicted")

    def __init__(self, max_size: int = 1000, persist_path: str | None = None) -> None:
        self._queue: deque[Any] = deque(maxlen=max_size)
        self._max_size = max_size
        self._persist_path: str = persist_path or os.getenv("HFT_FILL_DLQ_PERSIST_PATH") or _DEFAULT_PERSIST_PATH
        self._overflow_evicted: list[Any] = []

    def add(self, fill_event: Any) -> None:
        if len(self._queue) == self._max_size:
            MetricsRegistry.get().fill_dlq_overflow_total.inc()
            # Retain evicted fill in memory so persist() includes it.
            evicted = self._queue[0]
            self._overflow_evicted.append(evicted)
            # Also append to disk as immediate safety net in case of crash.
            self._persist_single(evicted)
            logger.error(
                "fill_dlq_overflow — evicted fill retained for persist",
                evicted_symbol=getattr(evicted, "symbol", ""),
                evicted_order_id=getattr(evicted, "order_id", ""),
                new_symbol=getattr(fill_event, "symbol", ""),
                dlq_size=len(self._queue),
                overflow_evicted=len(self._overflow_evicted),
            )
        self._queue.append(fill_event)
        logger.warning(
            "Orphaned fill added to DLQ", symbol=getattr(fill_event, "symbol", ""), dlq_size=len(self._queue)
        )

    def _persist_single(self, fill_event: Any) -> None:
        """Append a single fill to the DLQ file (best-effort, no fsync)."""
        try:
            import orjson

            row = asdict(fill_event)
            path = self._persist_path
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "ab") as f:
                f.write(orjson.dumps(row) + b"\n")
        except Exception as exc:
            logger.error("fill_dlq_persist_single_failed", error=str(exc))

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
        Includes overflow-evicted fills that were too old for the in-memory deque.
        """
        path = self._persist_path
        snapshot = list(self._overflow_evicted) + list(self._queue)
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
            # M2 (2026-04-25): finally-cleanup so orphan tmpfiles don't
            # accumulate when the worker dies between fsync and rename.
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
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
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
