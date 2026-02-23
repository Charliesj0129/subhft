"""AlphaWeightScheduler — periodic async refresh of alpha pool weights.

Runs an asyncio background loop that calls ``optimize_pool_weights`` every
``HFT_ALPHA_WEIGHT_REFRESH_S`` seconds and atomically applies new weights to the
pool object.  Follows the immutable-update pattern: ``set_weights`` on the pool
must replace the internal weights dict, never mutate in-place.

Environment variables
---------------------
HFT_ALPHA_WEIGHT_REFRESH_S : float, default 3600
    Refresh interval in seconds.  Set to ``0`` to disable.

Example
-------
::

    scheduler = AlphaWeightScheduler(pool=my_pool)
    scheduler.start()   # fire-and-forget asyncio task
    ...
    scheduler.stop()
"""

from __future__ import annotations

import asyncio
import os
from typing import Protocol, runtime_checkable

from structlog import get_logger

from hft_platform.alpha.pool import PoolOptimizationResult, optimize_pool_weights

logger = get_logger("alpha.weight_scheduler")


@runtime_checkable
class WeightedPool(Protocol):
    """Minimal protocol for an alpha pool that accepts weight updates."""

    def set_weights(self, weights: dict[str, float]) -> None:
        """Replace pool weights atomically (immutable update pattern)."""
        ...


class AlphaWeightScheduler:
    """Periodically refresh alpha pool weights via IC-weighted optimisation.

    Args:
        pool:       Object implementing :class:`WeightedPool` (must have
                    ``set_weights``).
        interval_s: Override refresh interval.  If *None*, reads
                    ``HFT_ALPHA_WEIGHT_REFRESH_S`` (default 3600).
                    Pass ``0`` to disable.
        base_dir:   Experiment tracker root passed to ``optimize_pool_weights``.
    """

    def __init__(
        self,
        pool: WeightedPool,
        interval_s: float | None = None,
        base_dir: str = "research/experiments",
    ) -> None:
        self._pool = pool
        env_s = float(os.getenv("HFT_ALPHA_WEIGHT_REFRESH_S", "3600"))
        self._interval: float = interval_s if interval_s is not None else env_s
        self._base_dir = base_dir
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Schedule the background refresh loop as an asyncio task."""
        if self._interval <= 0:
            logger.info("alpha_weight_scheduler_disabled", reason="interval=0")
            return
        if self._task and not self._task.done():
            logger.warning("alpha_weight_scheduler_already_running")
            return
        self._task = asyncio.ensure_future(self._loop())
        logger.info("alpha_weight_scheduler_started", interval_s=self._interval)

    def stop(self) -> None:
        """Cancel the background task (idempotent)."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        logger.info("alpha_weight_scheduler_stopped")

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._refresh_once()

    async def _refresh_once(self) -> None:
        """Run one weight optimisation cycle (non-blocking via executor)."""
        loop = asyncio.get_event_loop()
        try:
            result: PoolOptimizationResult = await loop.run_in_executor(
                None,
                lambda: optimize_pool_weights(
                    base_dir=self._base_dir,
                    method="ic_weighted",
                ),
            )
        except Exception as exc:
            logger.error("alpha_weight_refresh_failed", error=str(exc))
            return

        if not result.weights:
            logger.warning(
                "alpha_weight_refresh_empty",
                diagnostics=result.diagnostics,
            )
            return

        # Atomic replacement — pool.set_weights must not mutate existing dicts.
        self._pool.set_weights(result.weights)
        logger.info(
            "alpha_weights_refreshed",
            method=result.method,
            n_alphas=len(result.weights),
            weights=result.weights,
        )
