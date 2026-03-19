"""CanaryAutoScheduler — periodic async evaluation of all active canaries.

Runs an asyncio background loop that calls ``evaluate_all`` every
``HFT_CANARY_AUTO_INTERVAL_S`` seconds, evaluating each active canary against
its guardrails and optionally applying decisions (escalation/rollback/graduation).

Environment variables
---------------------
HFT_CANARY_AUTO_INTERVAL_S : float, default 86400
    Evaluation interval in seconds.  Set to ``0`` to disable.
HFT_CANARY_AUTO_DRY_RUN : str, default "1"
    ``"1"`` = dry-run mode (evaluate only, never apply).
    ``"0"`` = live mode (evaluate and apply decisions).

Example
-------
::

    scheduler = CanaryAutoScheduler(monitor=my_monitor)
    scheduler.start()   # fire-and-forget asyncio task
    ...
    scheduler.stop()
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from structlog import get_logger

from hft_platform.alpha.canary import CanaryMonitor, CanaryStatus

logger = get_logger("alpha.canary_scheduler")

_DEFAULT_INTERVAL_S = 86400.0  # 24 hours
_DEFAULT_DRY_RUN = True


class CanaryAutoScheduler:
    """Periodically evaluate all active canaries and optionally apply decisions.

    Args:
        monitor:    :class:`CanaryMonitor` instance for evaluation and decision
                    application.
        interval_s: Override evaluation interval.  If *None*, reads
                    ``HFT_CANARY_AUTO_INTERVAL_S`` (default 86400).
                    Pass ``0`` to disable.
        dry_run:    Override dry-run flag.  If *None*, reads
                    ``HFT_CANARY_AUTO_DRY_RUN`` (default ``"1"`` = True).
    """

    def __init__(
        self,
        monitor: CanaryMonitor,
        interval_s: float | None = None,
        dry_run: bool | None = None,
    ) -> None:
        self._monitor = monitor

        env_interval = float(os.getenv("HFT_CANARY_AUTO_INTERVAL_S", str(_DEFAULT_INTERVAL_S)))
        self._interval: float = interval_s if interval_s is not None else env_interval

        env_dry_run = os.getenv("HFT_CANARY_AUTO_DRY_RUN", "1") == "1"
        self._dry_run: bool = dry_run if dry_run is not None else env_dry_run

        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def dry_run(self) -> bool:
        """Whether the scheduler is in dry-run mode."""
        return self._dry_run

    @property
    def interval(self) -> float:
        """Evaluation interval in seconds."""
        return self._interval

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Schedule the background evaluation loop as an asyncio task."""
        if self._interval <= 0:
            logger.info("canary_auto_scheduler_disabled", reason="interval=0")
            return
        if self._task and not self._task.done():
            logger.warning("canary_auto_scheduler_already_running")
            return
        self._task = asyncio.ensure_future(self._run_loop())
        logger.info(
            "canary_auto_scheduler_started",
            interval_s=self._interval,
            dry_run=self._dry_run,
        )

    def stop(self) -> None:
        """Cancel the background task (idempotent)."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        logger.info("canary_auto_scheduler_stopped")

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Sleep-then-evaluate loop; runs until cancelled."""
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.evaluate_all()
            except Exception as _exc:  # noqa: BLE001
                logger.error("canary_auto_evaluate_loop_error", exc_info=True)

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    async def evaluate_all(self) -> list[CanaryStatus]:
        """Evaluate all active canaries and optionally apply decisions.

        For each active canary YAML, this method:
        1. Builds a metrics dict from the YAML state (sessions_live, sharpe_live
           if available; slippage/drawdown/error default to 0 when not stored).
        2. Calls ``monitor.evaluate()`` to produce a :class:`CanaryStatus`.
        3. If not dry-run, calls ``monitor.apply_decision()`` to modify config.

        Individual canary failures are logged and skipped (do not stop the loop).

        Returns:
            List of :class:`CanaryStatus` results for all evaluated canaries.
        """
        canaries = self._monitor.load_active_canaries()
        results: list[CanaryStatus] = []

        for canary in canaries:
            alpha_id = canary.get("alpha_id")
            if not alpha_id:
                logger.warning("canary_auto_skip_no_id", canary_keys=list(canary.keys()))
                continue

            try:
                live_metrics = self._build_metrics(canary)
                status = self._monitor.evaluate(str(alpha_id), live_metrics)
                results.append(status)

                logger.info(
                    "canary_auto_evaluated",
                    alpha_id=alpha_id,
                    state=status.state,
                    reason=status.reason,
                    dry_run=self._dry_run,
                )

                if not self._dry_run:
                    self._monitor.apply_decision(status)
                    logger.info(
                        "canary_auto_applied",
                        alpha_id=alpha_id,
                        state=status.state,
                    )
            except Exception as _exc:  # noqa: BLE001
                logger.error(
                    "canary_auto_evaluate_error",
                    alpha_id=alpha_id,
                    exc_info=True,
                )

        logger.info(
            "canary_auto_evaluate_all_done",
            total=len(canaries),
            evaluated=len(results),
            dry_run=self._dry_run,
        )
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_metrics(canary: dict[str, Any]) -> dict[str, Any]:
        """Extract live metrics from canary YAML state.

        The YAML may contain a ``live_metrics`` block with recorded values.
        Missing fields default to safe zeros so ``evaluate()`` treats them as
        passing.
        """
        stored = canary.get("live_metrics", {})
        if not isinstance(stored, dict):
            stored = {}

        metrics: dict[str, Any] = {
            "slippage_bps": float(stored.get("slippage_bps", 0.0)),
            "drawdown_contribution": float(stored.get("drawdown_contribution", 0.0)),
            "execution_error_rate": float(stored.get("execution_error_rate", 0.0)),
            "sessions_live": int(stored.get("sessions_live", 0)),
        }
        if "sharpe_live" in stored:
            metrics["sharpe_live"] = float(stored["sharpe_live"])

        return metrics
