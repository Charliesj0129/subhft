from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.execution.positions import PositionStore
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.risk.storm_guard import StormGuard

logger = get_logger("reconciliation")

# ---------------------------------------------------------------------------
# Environment-configurable resilience defaults (WU-04)
# ---------------------------------------------------------------------------
_DEFAULT_CHECK_INTERVAL_S = float(os.environ.get("HFT_RECON_CHECK_INTERVAL", "5"))  # precision-ok
_DEFAULT_GRACE_FAILURES = int(os.environ.get("HFT_RECON_GRACE_FAILURES", "10"))
_DEFAULT_BACKOFF_BASE = float(os.environ.get("HFT_RECON_BACKOFF_BASE", "2"))  # precision-ok
_DEFAULT_BACKOFF_MAX = float(os.environ.get("HFT_RECON_BACKOFF_MAX", "60"))  # precision-ok
_BACKOFF_JITTER = 0.2


@dataclass(slots=True)
class PositionDiscrepancy:
    """Represents a mismatch between local and broker positions."""

    symbol: str
    local_qty: int
    broker_qty: int
    diff: int

    @property
    def is_critical(self) -> bool:
        """Critical if signs differ or absolute diff exceeds threshold."""
        if self.local_qty == 0 and self.broker_qty == 0:
            return False
        # Sign mismatch is always critical
        if (self.local_qty > 0 and self.broker_qty < 0) or (self.local_qty < 0 and self.broker_qty > 0):
            return True
        # Large absolute diff is critical (threshold: 100 shares or 10% of position)
        threshold = max(100, abs(self.local_qty) // 10) if self.local_qty != 0 else 100
        return abs(self.diff) > threshold

    @property
    def severity(self) -> str:
        """Return severity label for metrics: critical, warning, or info."""
        if self.is_critical:
            return "critical"
        if abs(self.diff) > 10:
            return "warning"
        return "info"


def _compute_backoff_delay(
    attempt: int,
    base: float,  # precision-ok
    max_delay: float,  # precision-ok
    jitter: float,  # precision-ok
) -> float:
    """Compute exponential backoff delay with jitter.

    ``attempt`` is 0-indexed (first failure = attempt 0).
    """
    raw = min(base ** (attempt + 1), max_delay)
    jitter_factor = random.uniform(1 - jitter, 1 + jitter)
    return raw * jitter_factor


class ReconciliationService:
    def __init__(
        self,
        client: Any,
        position_store: PositionStore,
        config: dict,
        storm_guard: StormGuard,
    ) -> None:
        self.client = client
        self.store = position_store
        self.config = config
        self.storm_guard = storm_guard

        recon_cfg = config.get("reconciliation", {})

        # WU-04: resilient defaults
        self.check_interval_s: float = recon_cfg.get(  # precision-ok
            "check_interval_s",
            _DEFAULT_CHECK_INTERVAL_S,
        )
        self.grace_failures: int = recon_cfg.get(
            "grace_failures",
            _DEFAULT_GRACE_FAILURES,
        )
        self.backoff_base: float = recon_cfg.get(  # precision-ok
            "backoff_base",
            _DEFAULT_BACKOFF_BASE,
        )
        self.backoff_max: float = recon_cfg.get(  # precision-ok
            "backoff_max",
            _DEFAULT_BACKOFF_MAX,
        )

        self.last_heartbeat: float = timebase.now_s()  # precision-ok
        self.running: bool = False
        self._last_discrepancies: List[PositionDiscrepancy] = []
        self._consecutive_failures: int = 0
        self._halt_triggered: bool = False

    # ------------------------------------------------------------------
    # Metrics helpers (WU-18)
    # ------------------------------------------------------------------

    @staticmethod
    def _metrics() -> MetricsRegistry:
        return MetricsRegistry.get()

    def _record_sync_result(self, result: str) -> None:
        self._metrics().reconciliation_sync_total.labels(result=result).inc()

    def _record_sync_duration(self, duration_s: float) -> None:  # precision-ok
        self._metrics().reconciliation_sync_duration_seconds.observe(duration_s)

    def _record_discrepancy(self, severity: str) -> None:
        self._metrics().reconciliation_discrepancy_total.labels(severity=severity).inc()

    def _update_failure_gauge(self) -> None:
        self._metrics().reconciliation_consecutive_failures.set(self._consecutive_failures)

    def _update_last_success_ts(self) -> None:
        self._metrics().reconciliation_last_success_ts.set(time.time())

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self.running = True
        logger.info("ReconciliationService started")

        # 1. Startup Sync
        await self.sync_portfolio()

        while self.running:
            await asyncio.sleep(self.check_interval_s)

            # 2. Runtime Check - periodic reconciliation
            try:
                await self.sync_portfolio()
                # Reset on success (WU-04)
                self._consecutive_failures = 0
                self._update_failure_gauge()
            except Exception as e:
                self._consecutive_failures += 1
                self._update_failure_gauge()
                remaining = self.grace_failures - self._consecutive_failures

                logger.error(
                    "Runtime reconciliation failed",
                    error=str(e),
                    consecutive_failures=self._consecutive_failures,
                    grace_failures=self.grace_failures,
                    remaining_before_halt=max(remaining, 0),
                )

                if self._consecutive_failures >= self.grace_failures and not self._halt_triggered:
                    reason = f"RECONCILIATION_UNAVAILABLE: {self._consecutive_failures} consecutive failures"
                    self._halt_triggered = True
                    logger.critical(
                        "Triggering HALT due to reconciliation unavailability",
                        consecutive_failures=self._consecutive_failures,
                    )
                    if self.storm_guard:
                        self.storm_guard.trigger_halt(reason)
                    else:
                        logger.error("No StormGuard configured - HALT not triggered (manual intervention required)")
                else:
                    # Exponential backoff before next retry (WU-04)
                    delay = _compute_backoff_delay(
                        attempt=self._consecutive_failures - 1,
                        base=self.backoff_base,
                        max_delay=self.backoff_max,
                        jitter=_BACKOFF_JITTER,
                    )
                    logger.warning(
                        "Reconciliation failure countdown",
                        failure=self._consecutive_failures,
                        grace_failures=self.grace_failures,
                        next_retry_seconds=round(delay, 2),
                    )
                    await asyncio.sleep(delay)

    async def sync_portfolio(self) -> None:
        logger.info("Starting Portfolio Sync...")
        t0 = time.monotonic()
        try:
            # 1. Fetch positions from broker
            raw_positions = await asyncio.to_thread(self.client.get_positions)

            # 2. Build broker position map {symbol: qty}
            broker_map: Dict[str, int] = {}
            for pos in raw_positions:
                code = getattr(pos, "code", None) or (pos.get("code") if isinstance(pos, dict) else None)
                qty = getattr(pos, "quantity", None) or (pos.get("quantity", 0) if isinstance(pos, dict) else 0)
                direction = getattr(pos, "direction", "")
                if str(direction) == "Action.Sell":
                    qty = -qty
                if code:
                    broker_map[code] = int(qty)

            logger.info("Portfolio Sync: Broker State", positions=broker_map)

            # 3. Build local position map {symbol: qty}
            local_map: Dict[str, int] = {}
            for key, pos in self.store.positions.items():
                symbol = pos.symbol
                local_map[symbol] = local_map.get(symbol, 0) + pos.net_qty

            logger.info("Portfolio Sync: Local State", positions=local_map)

            # 4. Compute discrepancies
            discrepancies = self._compute_discrepancies(local_map, broker_map)
            self._last_discrepancies = discrepancies

            # 5. Update reconciliation discrepancy metric (legacy)
            self._metrics().reconciliation_discrepancy_count.set(len(discrepancies))

            # 6. Record per-severity discrepancy metrics (WU-18)
            for d in discrepancies:
                self._record_discrepancy(d.severity)

            # 7. Duration + success metrics
            duration = time.monotonic() - t0
            self._record_sync_duration(duration)
            self._record_sync_result("success")
            self._update_last_success_ts()

            if discrepancies:
                logger.warning(
                    "Position discrepancies detected",
                    count=len(discrepancies),
                    discrepancies=[
                        {"symbol": d.symbol, "local": d.local_qty, "broker": d.broker_qty, "diff": d.diff}
                        for d in discrepancies
                    ],
                )

                # 8. Check for critical discrepancies and trigger HALT if needed
                critical = [d for d in discrepancies if d.is_critical]
                if critical:
                    await self._trigger_halt(critical)
            else:
                logger.info("Portfolio Sync Complete - No discrepancies", count=len(broker_map))

        except Exception as e:
            duration = time.monotonic() - t0
            self._record_sync_duration(duration)
            self._record_sync_result("failure")
            logger.error("Portfolio Sync Failed", error=str(e), exc_info=True)
            raise

    def _compute_discrepancies(
        self, local_map: Dict[str, int], broker_map: Dict[str, int]
    ) -> List[PositionDiscrepancy]:
        """Compare local and broker positions, return list of discrepancies."""
        discrepancies: List[PositionDiscrepancy] = []
        all_symbols = set(local_map.keys()) | set(broker_map.keys())

        for symbol in all_symbols:
            local_qty = local_map.get(symbol, 0)
            broker_qty = broker_map.get(symbol, 0)
            diff = local_qty - broker_qty

            if diff != 0:
                discrepancies.append(
                    PositionDiscrepancy(
                        symbol=symbol,
                        local_qty=local_qty,
                        broker_qty=broker_qty,
                        diff=diff,
                    )
                )

        return discrepancies

    async def _trigger_halt(self, critical_discrepancies: List[PositionDiscrepancy]) -> None:
        """Trigger StormGuard HALT due to reconciliation mismatch."""
        symbols = [d.symbol for d in critical_discrepancies]
        reason = f"RECONCILIATION_MISMATCH: {len(critical_discrepancies)} critical discrepancies ({symbols[:3]})"

        logger.critical(
            "Triggering HALT due to reconciliation mismatch",
            critical_count=len(critical_discrepancies),
            symbols=symbols,
        )

        self.storm_guard.trigger_halt(reason)
