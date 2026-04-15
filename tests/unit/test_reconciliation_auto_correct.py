"""Tests for ReconciliationService position drift auto-correction.

Verifies that persistent phantom order drift (local=0, broker=N) is
automatically corrected after sufficient consecutive observations.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.positions import PositionStore
from hft_platform.execution.reconciliation import (
    PositionDiscrepancy,
    ReconciliationService,
)
from hft_platform.risk.storm_guard import StormGuard, StormGuardState


@pytest.fixture
def guard():
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        g = StormGuard()
        g._halt_cooldown_s = 0.0
        g._storm_cooldown_s = 0.0
        g._de_escalate_threshold = 1
        yield g


@pytest.fixture
def service(guard):
    client = MagicMock()
    # Broker consistently reports 1 lot of TMFD6
    client.get_positions.return_value = [
        SimpleNamespace(code="TMFD6", quantity=1, direction="Long"),
    ]
    store = PositionStore()
    svc = ReconciliationService(
        client,
        store,
        {
            "reconciliation": {
                "check_interval_s": 0.01,
                "broker_zero_debounce_observations": 1,
            },
        },
        storm_guard=guard,
    )
    # Speed up auto-correct: trigger after 3 observations (not 5)
    svc._auto_correct_after = 3
    svc._critical_drift_debounce = 1  # trigger HALT on first observation
    return svc


@pytest.mark.asyncio
async def test_auto_correct_adopts_broker_position(service, guard):
    """After sufficient drift observations, auto-correct adopts broker state."""
    # Observation 1: triggers HALT + reconciliation hold
    await service.sync_portfolio()
    assert guard.state == StormGuardState.HALT
    assert guard.reconciliation_hold is True
    assert service._halt_triggered is True

    # Observations 2-3: critical drift persists during HALT
    await service.sync_portfolio()
    assert service._critical_drift_streak == 2

    # Observation 3 (streak=3 >= auto_correct_after=3): auto-correct fires
    await service.sync_portfolio()

    # After auto-correct: drift state is reset, hold is released
    assert service._halt_triggered is False
    assert service._critical_drift_streak == 0
    assert guard.reconciliation_hold is False

    # The position should now be loaded into the store
    recovery = getattr(service.store, "_recovery_positions", {})
    tmfd6_entry = None
    for key, data in recovery.items():
        sym = data.get("symbol") if isinstance(data, dict) else getattr(data, "symbol", "")
        if sym == "TMFD6":
            tmfd6_entry = data
            break
    assert tmfd6_entry is not None
    qty = tmfd6_entry.get("net_qty") if isinstance(tmfd6_entry, dict) else tmfd6_entry.net_qty
    assert qty == 1  # adopted from broker


@pytest.mark.asyncio
async def test_auto_correct_only_when_local_is_zero(service, guard):
    """Auto-correct only fires when local=0 (phantom order scenario)."""
    # Give local a position that differs from broker (local=2, broker=1)
    service.store.load_recovery(
        account_id="default",
        symbol="TMFD6",
        net_qty=2,
        avg_price_scaled=100_0000,
        realized_pnl_scaled=0,
        fees_scaled=0,
        strategy_id="r47",
    )

    # Trigger HALT (streak 1)
    await service.sync_portfolio()
    assert guard.state == StormGuardState.HALT

    # Observations 2-4 (past auto_correct_after=3)
    for _ in range(3):
        await service.sync_portfolio()

    # Auto-correct should NOT fire because local != 0
    # The halt_triggered should still be True (drift persists)
    assert service._halt_triggered is True


@pytest.mark.asyncio
async def test_auto_correct_respects_futures_max_qty(service, guard):
    """Auto-correct respects max qty threshold for futures."""
    # Broker has 5 lots — exceeds default max of 2
    service.client.get_positions.return_value = [
        SimpleNamespace(code="TXFD6", quantity=5, direction="Long"),
    ]
    service._auto_correct_futures_max_qty = 2

    # Run enough observations
    for _ in range(5):
        await service.sync_portfolio()

    # Should NOT auto-correct because 5 > 2
    assert service._halt_triggered is True


@pytest.mark.asyncio
async def test_auto_correct_disabled_by_env(guard):
    """Auto-correct can be disabled via config."""
    client = MagicMock()
    client.get_positions.return_value = [
        SimpleNamespace(code="TMFD6", quantity=1, direction="Long"),
    ]
    store = PositionStore()
    svc = ReconciliationService(client, store, {}, storm_guard=guard)
    svc._auto_correct_enabled = False
    svc._auto_correct_after = 1
    svc._critical_drift_debounce = 1

    for _ in range(5):
        await svc.sync_portfolio()

    # HALT was triggered but auto-correct never fires
    assert svc._halt_triggered is True
    assert svc._critical_drift_streak == 5


@pytest.mark.asyncio
async def test_reconciliation_hold_released_on_drift_resolved(service, guard):
    """When drift resolves naturally, reconciliation hold is released."""
    # Trigger HALT
    await service.sync_portfolio()
    assert guard.reconciliation_hold is True

    # Now broker reports 0 (position closed) — same as local
    service.client.get_positions.return_value = []
    await service.sync_portfolio()

    # No discrepancies — hold should be released
    assert guard.reconciliation_hold is False
    assert service._halt_triggered is False


def test_filter_auto_correctable_local_zero_only():
    """_filter_auto_correctable only includes local=0 discrepancies."""
    svc = MagicMock(spec=ReconciliationService)
    svc._auto_correct_futures_max_qty = 2
    svc._auto_correct_stock_max_qty = 10

    discrepancies = [
        PositionDiscrepancy("TMFD6", local_qty=0, broker_qty=1, diff=-1, is_futures=True),
        PositionDiscrepancy("TXFD6", local_qty=2, broker_qty=3, diff=-1, is_futures=True),
        PositionDiscrepancy("2330", local_qty=0, broker_qty=5, diff=-5, is_futures=False),
    ]

    result = ReconciliationService._filter_auto_correctable(svc, discrepancies)
    symbols = [d.symbol for d in result]
    assert "TMFD6" in symbols  # local=0, qty=1 <= 2
    assert "TXFD6" not in symbols  # local=2, not 0
    assert "2330" in symbols  # local=0, qty=5 <= 10
