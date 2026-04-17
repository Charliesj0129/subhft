"""Bug 27 (2026-04-17) — PositionStuckMonitor behaviour.

Why this matters:
    Today's R47 TMFE6 deadlock (37478 SELL → 37819 BUY cover, -3,410 NTD) had
    NO real-time alert. Operator only caught it via manual cron. This monitor
    closes that observability gap by:
      * Exposing ``hft_position_age_seconds{strategy,symbol}`` gauge
      * Telegram alerting on age > threshold with dedup
      * Clearing gauge + dedup when position goes flat
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.ops.position_stuck_monitor import PositionStuckMonitor


def _pos(strategy_id, symbol, net_qty, last_update_ts, avg_price_scaled=377_000_000):
    return SimpleNamespace(
        strategy_id=strategy_id,
        symbol=symbol,
        net_qty=net_qty,
        avg_price_scaled=avg_price_scaled,
        last_update_ts=last_update_ts,
    )


class _Store:
    def __init__(self, positions):
        # Mimic PositionStore.positions: Dict[key, Position]
        self.positions = {
            f"{p.strategy_id}:{p.symbol}": p for p in positions
        }


@pytest.mark.asyncio
class TestPositionStuckMonitor:
    async def test_flat_position_emits_no_alert(self):
        now_ns = time.time_ns()
        store = _Store([_pos("R47", "TMFE6", 0, now_ns - 10_000_000_000_000)])  # flat
        dispatcher = AsyncMock()
        mon = PositionStuckMonitor(
            position_store=store,
            dispatcher=dispatcher,
            alert_threshold_s=5,
        )
        await mon._tick()
        dispatcher.notify_position_stuck.assert_not_awaited()

    async def test_young_position_updates_gauge_no_alert(self):
        now_ns = time.time_ns()
        store = _Store([_pos("R47", "TMFE6", -1, now_ns - 2_000_000_000)])  # 2s old
        dispatcher = AsyncMock()
        mon = PositionStuckMonitor(
            position_store=store,
            dispatcher=dispatcher,
            alert_threshold_s=300,
        )
        await mon._tick()
        dispatcher.notify_position_stuck.assert_not_awaited()

        # Gauge populated
        metric = MetricsRegistry.get().position_age_seconds.labels(
            strategy="R47", symbol="TMFE6"
        )
        assert metric._value.get() >= 0

    async def test_stuck_position_fires_once_then_dedups(self):
        # 10 min old position
        now_ns = time.time_ns()
        stuck = _pos("R47", "TMFE6", -1, now_ns - 600_000_000_000)
        store = _Store([stuck])
        dispatcher = AsyncMock()
        mon = PositionStuckMonitor(
            position_store=store,
            dispatcher=dispatcher,
            alert_threshold_s=300,
        )
        await mon._tick()
        await mon._tick()  # second tick must NOT re-alert (dedup)
        await mon._tick()

        assert dispatcher.notify_position_stuck.await_count == 1
        call = dispatcher.notify_position_stuck.await_args
        assert call.kwargs["strategy_id"] == "R47"
        assert call.kwargs["symbol"] == "TMFE6"
        assert call.kwargs["net_qty"] == -1
        assert call.kwargs["age_s"] >= 300

    async def test_position_going_flat_rearms_alert(self):
        now_ns = time.time_ns()
        stuck = _pos("R47", "TMFE6", -1, now_ns - 600_000_000_000)
        store = _Store([stuck])
        dispatcher = AsyncMock()
        mon = PositionStuckMonitor(
            position_store=store,
            dispatcher=dispatcher,
            alert_threshold_s=300,
        )
        await mon._tick()
        assert dispatcher.notify_position_stuck.await_count == 1

        # Position flips to flat → dedup key cleared
        stuck.net_qty = 0
        await mon._tick()

        # New stuck position for same key
        stuck.net_qty = -1
        stuck.last_update_ts = time.time_ns() - 600_000_000_000
        await mon._tick()

        assert dispatcher.notify_position_stuck.await_count == 2

    async def test_multiple_positions_independent(self):
        now_ns = time.time_ns()
        old = _pos("R47", "TMFE6", -1, now_ns - 600_000_000_000)
        young = _pos("C14", "TXFF6", 1, now_ns - 5_000_000_000)
        store = _Store([old, young])
        dispatcher = AsyncMock()
        mon = PositionStuckMonitor(
            position_store=store,
            dispatcher=dispatcher,
            alert_threshold_s=300,
        )
        await mon._tick()
        assert dispatcher.notify_position_stuck.await_count == 1
        call = dispatcher.notify_position_stuck.await_args
        assert call.kwargs["strategy_id"] == "R47"

    async def test_missing_dispatcher_is_noop(self):
        """Alert-only mode without dispatcher must not raise."""
        now_ns = time.time_ns()
        store = _Store([_pos("R47", "TMFE6", -1, now_ns - 600_000_000_000)])
        mon = PositionStuckMonitor(
            position_store=store,
            dispatcher=None,
            alert_threshold_s=300,
        )
        await mon._tick()  # must not raise

    async def test_unrealized_pnl_computed_when_mid_provided(self):
        """Verify that mid_price_fn yields an unrealized_ntd field in the alert."""
        now_ns = time.time_ns()
        # net_qty=-1 short @ 37800*10000, mid=37900*10000 → diff +100*10000 scaled
        # unrealized = (37900-37800)*10000 * -1 * 10 / 10000 = -1000 NTD
        stuck = _pos("R47", "TMFE6", -1, now_ns - 600_000_000_000,
                     avg_price_scaled=378_000_000)
        store = _Store([stuck])
        dispatcher = AsyncMock()
        mon = PositionStuckMonitor(
            position_store=store,
            dispatcher=dispatcher,
            mid_price_fn=lambda _sym: 379_000_000,
            alert_threshold_s=300,
        )
        await mon._tick()
        call = dispatcher.notify_position_stuck.await_args
        assert call.kwargs["unrealized_ntd"] == -1000
