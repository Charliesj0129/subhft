"""PositionStuckMonitor — Bug 27 (2026-04-17) observability gap closure.

Background: today's 37478 SELL → 37819 BUY cover deadlock (-3,410 NTD, R47
TMFE6) had NO real-time alert. The operator only discovered it via manual
cron-based monitoring. This monitor closes that gap.

Behaviour:
    * Polls PositionStore every ``interval_s`` seconds (default 5s).
    * For each position with ``net_qty != 0``, computes age =
      ``time.time() - last_update_ts`` and updates the Prometheus gauge
      ``hft_position_age_seconds{strategy,symbol}``.
    * When age crosses ``alert_threshold_s`` (default 300s, i.e. 5 min), emits a
      Telegram alert via ``NotificationDispatcher.notify_position_stuck`` with
      dedup. Dedup is internal here too so the async dispatcher isn't spammed.
    * When the position goes flat (net_qty → 0), the gauge is removed and the
      dedup is cleared so the next stuck episode re-alerts.

Design notes:
    * Strictly alert-only. Auto-flatten stays behind ``HFT_AUTONOMY_MONITOR_ENABLED``
      + ``HFT_HALT_AUTO_FLATTEN`` — this monitor never places orders.
    * Independent of AutonomyMonitor (which is opt-in). Wiring PositionStuckMonitor
      does not require broker_disconnect / margin / flatten_gate infra to be live.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from structlog import get_logger

from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("ops.position_stuck_monitor")


class PositionStuckMonitor:
    """Polls PositionStore for non-flat positions with stale last_update_ts."""

    __slots__ = (
        "_position_store",
        "_dispatcher",
        "_mid_price_fn",
        "_metrics",
        "_interval_s",
        "_alert_threshold_s",
        "_running",
        "_task",
        "_alerted_keys",
        "_seen_keys",
        "_first_observed_ns",
        "_get_contract_multiplier",
    )

    def __init__(
        self,
        *,
        position_store: Any,
        dispatcher: Any | None = None,
        mid_price_fn: Any = None,
        interval_s: float | None = None,
        alert_threshold_s: float | None = None,
        contract_multiplier_fn: Any = None,
    ) -> None:
        self._position_store = position_store
        self._dispatcher = dispatcher
        self._mid_price_fn = mid_price_fn
        self._metrics = MetricsRegistry.get()
        self._interval_s = float(interval_s if interval_s is not None else os.getenv("HFT_POSITION_STUCK_POLL_S", "5"))
        self._alert_threshold_s = float(
            alert_threshold_s if alert_threshold_s is not None else os.getenv("HFT_POSITION_STUCK_ALERT_S", "300")
        )
        self._running = False
        self._task: asyncio.Task[None] | None = None
        # (strategy_id, symbol) keys currently in "alerted" state (dedup)
        self._alerted_keys: set[tuple[str, str]] = set()
        # (strategy_id, symbol) keys we have observed non-flat so we can clean gauges
        self._seen_keys: set[tuple[str, str]] = set()
        # Bug 27b (2026-04-18): track first-observed non-zero time per key so that
        # positions restored via startup-recovery (with stale ``last_update_ts``
        # from a prior engine run) don't generate false 24h age alerts. We use
        # the MAX of ``last_update_ts`` and ``first_observed_ns`` as the age
        # reference — new fills (which update last_update_ts) still take effect.
        self._first_observed_ns: dict[tuple[str, str], int] = {}
        self._get_contract_multiplier = contract_multiplier_fn

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Supervised coroutine entry point (matches AutonomyMonitor.run signature)."""
        self._running = True
        logger.info(
            "position_stuck_monitor_started",
            interval_s=self._interval_s,
            alert_threshold_s=self._alert_threshold_s,
        )
        await self._loop()

    async def start(self) -> None:
        """Create a standalone task (legacy/test-friendly API)."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="position_stuck_monitor")
        logger.info(
            "position_stuck_monitor_started",
            interval_s=self._interval_s,
            alert_threshold_s=self._alert_threshold_s,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("position_stuck_monitor_error", error=str(exc))
            await asyncio.sleep(self._interval_s)

    # ------------------------------------------------------------------
    # Core logic (pure-ish — easy to unit test)
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        """One evaluation pass. Exposed for deterministic unit tests."""
        positions = getattr(self._position_store, "positions", {}) or {}
        now_ns = time.time_ns()

        current_keys: set[tuple[str, str]] = set()

        for pos in list(positions.values()):
            net_qty = int(getattr(pos, "net_qty", 0) or 0)
            if net_qty == 0:
                continue

            strategy_id = str(getattr(pos, "strategy_id", "") or "")
            symbol = str(getattr(pos, "symbol", "") or "")
            if not strategy_id or not symbol:
                continue

            last_update_ts = int(getattr(pos, "last_update_ts", 0) or 0)
            key = (strategy_id, symbol)
            current_keys.add(key)
            self._seen_keys.add(key)
            # Record the first tick we saw this non-zero position; used to
            # defeat stale ``last_update_ts`` inherited from startup recovery.
            if key not in self._first_observed_ns:
                self._first_observed_ns[key] = now_ns

            # Age reference: the LATER of fresh fill time vs first-observed time.
            # If last_update_ts is stale (recovery) but within this monitor's
            # lifetime we use first_observed_ns → position must actually persist
            # for ``alert_threshold_s`` within the live session before alerting.
            reference_ns = max(last_update_ts, self._first_observed_ns[key])
            age_s = max(0, (now_ns - reference_ns) // 1_000_000_000)

            if last_update_ts <= 0 and age_s < self._alert_threshold_s:
                # Recovery-state position with no fill yet and not long enough to alert — skip gauge too.
                continue

            try:
                self._metrics.position_age_seconds.labels(strategy=strategy_id, symbol=symbol).set(float(age_s))
            except Exception:  # noqa: BLE001 — metric failure must not block
                pass

            if age_s >= self._alert_threshold_s and key not in self._alerted_keys:
                await self._emit_alert(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    net_qty=net_qty,
                    avg_price_scaled=int(getattr(pos, "avg_price_scaled", 0) or 0),
                    age_s=int(age_s),
                )
                self._alerted_keys.add(key)

        # Clean up stale keys (position went flat or disappeared)
        stale = self._seen_keys - current_keys
        for key in stale:
            try:
                self._metrics.position_age_seconds.remove(*key)
            except Exception:  # noqa: BLE001 — label may never have been set
                pass
            self._alerted_keys.discard(key)
            self._seen_keys.discard(key)
            # Clear first-observed so the next non-flat episode re-anchors.
            self._first_observed_ns.pop(key, None)

    async def _emit_alert(
        self,
        *,
        strategy_id: str,
        symbol: str,
        net_qty: int,
        avg_price_scaled: int,
        age_s: int,
    ) -> None:
        unrealized_ntd = self._estimate_unrealized_ntd(symbol, net_qty, avg_price_scaled)
        logger.warning(
            "position_stuck_alert",
            strategy_id=strategy_id,
            symbol=symbol,
            net_qty=net_qty,
            age_s=age_s,
            unrealized_ntd=unrealized_ntd,
        )
        try:
            self._metrics.position_stuck_alerts_total.labels(strategy=strategy_id, symbol=symbol).inc()
        except Exception:  # noqa: BLE001
            pass

        if self._dispatcher is None:
            return
        try:
            await self._dispatcher.notify_position_stuck(
                strategy_id=strategy_id,
                symbol=symbol,
                net_qty=net_qty,
                age_s=age_s,
                unrealized_ntd=unrealized_ntd,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("position_stuck_notify_failed", error=str(exc))

    def _estimate_unrealized_ntd(self, symbol: str, net_qty: int, avg_price_scaled: int) -> int | None:
        """Best-effort unrealized PnL for the alert message. None if unavailable."""
        if self._mid_price_fn is None or avg_price_scaled == 0 or net_qty == 0:
            return None
        try:
            mid_scaled = int(self._mid_price_fn(symbol) or 0)
        except Exception:  # noqa: BLE001
            return None
        if mid_scaled <= 0:
            return None
        multiplier = 10  # Default for TMF mini; overridden if fn provided
        if self._get_contract_multiplier is not None:
            try:
                multiplier = int(self._get_contract_multiplier(symbol) or 10)
            except Exception:  # noqa: BLE001
                multiplier = 10
        # Scaled diff; mid_scaled and avg_price_scaled are both in the same fixed-point scale.
        # NTD ≈ (diff / scale) * qty * multiplier. Use 10000 as default scale (x10000 convention).
        try:
            diff = mid_scaled - avg_price_scaled
            # If caller uses the x10000 scale (per CLAUDE.md), divide by 10000.
            ntd = (diff * net_qty * multiplier) // 10000
            return int(ntd)
        except Exception:  # noqa: BLE001
            return None
