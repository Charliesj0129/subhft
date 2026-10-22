"""Fire-and-forget asyncio tasks that must not be garbage collected while they run.

``asyncio`` keeps only a weak reference to a task. A task that nobody else references and that is parked on a future
nobody else references is collected mid-flight, its coroutine is closed at the ``await`` and the work (a FORCE_FLAT
dispatch, a HALT or daily-loss notification) silently never completes. The documented remedy is to keep a strong
reference until the task is done; :func:`spawn_retained` does that, and also reports a failed task (log + counter)
instead of leaving "Task exception was never retrieved" for the garbage collector to print.

``name`` is both the task name and the ``background_task_failed_total`` label: use a fixed string, never an id.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Coroutine
from typing import Any

from structlog import get_logger

logger = get_logger("core.background_tasks")

_RETAINED: set[asyncio.Task[Any]] = set()


def spawn_retained(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str,
    loop: asyncio.AbstractEventLoop | None = None,
    **context: Any,
) -> asyncio.Task[Any]:
    """Schedule ``coro`` on ``loop`` (default: the running loop) and keep the task alive until it finishes.

    Raises ``RuntimeError`` when no loop is given and none is running, exactly like ``asyncio.create_task``; the
    caller's existing fallback for that case keeps working. ``context`` is logged if the task fails.
    """
    target = loop if loop is not None else asyncio.get_running_loop()
    task = target.create_task(coro, name=name)
    _RETAINED.add(task)
    task.add_done_callback(functools.partial(_on_done, context))
    return task


def retained_count() -> int:
    """Number of retained tasks still running (for tests and diagnostics)."""
    return len(_RETAINED)


def _on_done(context: dict[str, Any], task: asyncio.Task[Any]) -> None:
    _RETAINED.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    name = task.get_name()
    logger.error(
        "background_task_failed",
        task=name,
        error=str(exc),
        error_type=type(exc).__name__,
        exc_info=exc,
        **context,
    )
    try:
        from hft_platform.observability.metrics import MetricsRegistry

        MetricsRegistry.get().background_task_failed_total.labels(name=name).inc()
    except Exception:  # noqa: BLE001 -- the failure is already logged; a metrics fault must not raise in a callback
        logger.warning("background_task_failed_metric_unavailable", task=name, exc_info=True)
