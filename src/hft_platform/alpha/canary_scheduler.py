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
from hft_platform.core.background_tasks import spawn_retained

logger = get_logger("alpha.canary_scheduler")

_DEFAULT_INTERVAL_S = 86400.0
_DEFAULT_DRY_RUN = True

_FAILSAFE_SLIPPAGE_BPS = 999.0
_FAILSAFE_DRAWDOWN = 1.0
_FAILSAFE_ERROR_RATE = 1.0


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
        metrics_query: Any | None = None,
        notification_dispatcher: Any | None = None,
    ) -> None:
        self._monitor = monitor

        env_interval = float(os.getenv("HFT_CANARY_AUTO_INTERVAL_S", str(_DEFAULT_INTERVAL_S)))
        self._interval: float = interval_s if interval_s is not None else env_interval

        env_dry_run = os.getenv("HFT_CANARY_AUTO_DRY_RUN", "1") == "1"
        self._dry_run: bool = dry_run if dry_run is not None else env_dry_run

        self._metrics_query: Any | None = metrics_query
        self._notification_dispatcher: Any | None = notification_dispatcher

        self._task: asyncio.Task[None] | None = None

    @property
    def dry_run(self) -> bool:
        """Whether the scheduler is in dry-run mode."""
        return self._dry_run

    @property
    def interval(self) -> float:
        """Evaluation interval in seconds."""
        return self._interval

    def start(self) -> None:
        """Schedule the background evaluation loop as an asyncio task."""
        if self._interval <= 0:
            logger.info("canary_auto_scheduler_disabled", reason="interval=0")
            return
        if self._task and not self._task.done():
            logger.warning("canary_auto_scheduler_already_running")
            return
        self._task = asyncio.ensure_future(self._run_loop())
        self._task.add_done_callback(self._task_done_callback)
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

    @staticmethod
    def _task_done_callback(task: asyncio.Task[None]) -> None:
        """Log if the background task finished with an unexpected exception."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "canary_auto_scheduler_task_died",
                error=str(exc),
                exc_type=type(exc).__name__,
            )

    async def _run_loop(self) -> None:
        """Sleep-then-evaluate loop; runs until cancelled."""
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.evaluate_all()
            except Exception as _exc:
                logger.error("canary_auto_evaluate_loop_error", exc_info=True)

    async def evaluate_all(self) -> list[CanaryStatus]:
        """Evaluate all active canaries and optionally apply decisions.

        For each active canary YAML, this method:
        1. Builds a metrics dict from the YAML state (sessions_live, sharpe_live
           if available; slippage/drawdown/error default to fail-safe worst-case
           values when not stored, triggering rollback if data is missing).
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
                live_metrics = None
                _ck_actually_used = False
                if self._metrics_query is not None:
                    strategy_id = canary.get("strategy_id", str(alpha_id))
                    since_ns = int(canary.get("promoted_at_ns", 0))
                    live_metrics = self._metrics_query.fetch(
                        str(alpha_id),
                        str(strategy_id),
                        since_ns,
                    )
                    _ck_actually_used = live_metrics is not None
                if live_metrics is None:
                    live_metrics = self._build_metrics(canary)

                status = self._monitor.evaluate(str(alpha_id), live_metrics)
                results.append(status)

                _ck_used = _ck_actually_used
                logger.info(
                    "canary_auto_evaluated",
                    alpha_id=alpha_id,
                    state=status.state,
                    reason=status.reason,
                    dry_run=self._dry_run,
                    metrics_source="clickhouse" if _ck_used else "yaml_fallback",
                )

                if not self._dry_run:
                    self._monitor.apply_decision(status)
                    logger.info(
                        "canary_auto_applied",
                        alpha_id=alpha_id,
                        state=status.state,
                    )
                    if status.state in ("rolled_back", "graduated") and self._notification_dispatcher is not None:
                        try:
                            spawn_retained(
                                self._notification_dispatcher.notify_canary_action(
                                    alpha_id=str(alpha_id),
                                    action=status.state,
                                    reason=status.reason,
                                ),
                                name="notify_canary_action",
                            )
                        except Exception as _notify_exc:
                            logger.warning(
                                "canary_auto_notify_failed",
                                alpha_id=alpha_id,
                                error=str(_notify_exc),
                            )
            except Exception as _exc:
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

    @staticmethod
    def _build_metrics(canary: dict[str, Any]) -> dict[str, Any]:
        """Extract live metrics from canary YAML state.

        The YAML may contain a ``live_metrics`` block with recorded values.
        Missing fields default to fail-safe (worst-case) values, triggering
        rollback when data is missing. This is conservative: missing metrics
        are treated as worst-case performance.
        """
        stored = canary.get("live_metrics", {})
        if not isinstance(stored, dict):
            stored = {}

        if not stored:
            return {
                "slippage_bps": _FAILSAFE_SLIPPAGE_BPS,
                "drawdown_contribution": _FAILSAFE_DRAWDOWN,
                "execution_error_rate": _FAILSAFE_ERROR_RATE,
                "sessions_live": 0,
            }

        def _safe_float(val: Any, default: float) -> float:
            return float(val) if val is not None else default

        def _safe_int(val: Any, default: int) -> int:
            return int(val) if val is not None else default

        metrics: dict[str, Any] = {
            "slippage_bps": _safe_float(stored.get("slippage_bps"), _FAILSAFE_SLIPPAGE_BPS),
            "drawdown_contribution": _safe_float(stored.get("drawdown_contribution"), _FAILSAFE_DRAWDOWN),
            "execution_error_rate": _safe_float(stored.get("execution_error_rate"), _FAILSAFE_ERROR_RATE),
            "sessions_live": _safe_int(stored.get("sessions_live"), 0),
        }
        if "sharpe_live" in stored and stored["sharpe_live"] is not None:
            metrics["sharpe_live"] = float(stored["sharpe_live"])

        return metrics
