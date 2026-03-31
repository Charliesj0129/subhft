"""Dead-letter queue for orphaned fills (WU-03)."""

from collections import deque
from typing import Any

from structlog import get_logger

logger = get_logger("execution.fill_dlq")


class OrphanedFillDLQ:
    __slots__ = ("_queue", "_max_size")

    def __init__(self, max_size: int = 1000) -> None:
        self._queue: deque[Any] = deque(maxlen=max_size)
        self._max_size = max_size

    def add(self, fill_event: Any) -> None:
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


_dlq: OrphanedFillDLQ | None = None


def get_orphaned_fill_dlq() -> OrphanedFillDLQ:
    global _dlq
    if _dlq is None:
        _dlq = OrphanedFillDLQ()
    return _dlq
