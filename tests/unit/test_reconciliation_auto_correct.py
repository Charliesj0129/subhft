"""Tests for ReconciliationService position drift auto-correction.

Verifies that persistent phantom order drift (local=0, broker=N) is
automatically corrected after sufficient consecutive observations.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.constants import MANUAL_STRATEGY_ID
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


def test_filter_auto_correctable_either_side_zero():
    """_filter_auto_correctable handles both directions: local=0 and broker=0."""
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
    assert "TXFD6" not in symbols  # both non-zero
    assert "2330" in symbols  # local=0, qty=5 <= 10


def test_filter_auto_correctable_broker_zero_direction():
    """_filter_auto_correctable allows broker=0 (expired/cleared position)."""
    svc = MagicMock(spec=ReconciliationService)
    svc._auto_correct_futures_max_qty = 2
    svc._auto_correct_stock_max_qty = 10

    discrepancies = [
        # Expired option: local=1, broker=0
        PositionDiscrepancy("TX438500D6", local_qty=1, broker_qty=0, diff=1, is_futures=True),
        # Large position mismatch: local=5, broker=0 — exceeds threshold
        PositionDiscrepancy("TXFE6", local_qty=5, broker_qty=0, diff=5, is_futures=True),
        # Stock: local=3, broker=0 — within stock threshold
        PositionDiscrepancy("2330", local_qty=3, broker_qty=0, diff=3, is_futures=False),
    ]

    result = ReconciliationService._filter_auto_correctable(svc, discrepancies)
    symbols = [d.symbol for d in result]
    assert "TX438500D6" in symbols  # broker=0, local=1 <= 2
    assert "TXFE6" not in symbols  # broker=0 but local=5 > 2 (futures max)
    assert "2330" in symbols  # broker=0, local=3 <= 10 (stock max)


def test_filter_auto_correctable_both_nonzero_rejected():
    """Both sides non-zero is never auto-correctable (requires manual intervention)."""
    svc = MagicMock(spec=ReconciliationService)
    svc._auto_correct_futures_max_qty = 10
    svc._auto_correct_stock_max_qty = 100

    discrepancies = [
        PositionDiscrepancy("TMFE6", local_qty=2, broker_qty=1, diff=1, is_futures=True),
        PositionDiscrepancy("TXFE6", local_qty=-1, broker_qty=1, diff=-2, is_futures=True),
    ]

    result = ReconciliationService._filter_auto_correctable(svc, discrepancies)
    assert result == []


@pytest.mark.asyncio
async def test_auto_correct_clears_phantom_local_position(guard):
    """Auto-correct clears phantom local position when broker reports 0."""
    client = MagicMock()
    # Broker reports NO positions (option expired)
    client.get_positions.return_value = []
    store = PositionStore()
    # Seed a phantom local position (from expired option loaded at startup)
    store.load_recovery(
        account_id="default",
        symbol="TX438500D6",
        net_qty=1,
        avg_price_scaled=100_0000,
        strategy_id=MANUAL_STRATEGY_ID,
    )

    svc = ReconciliationService(client, store, {}, storm_guard=guard)
    svc._auto_correct_after = 2
    svc._critical_drift_debounce = 1
    # Need at least 2 broker-zero observations before discrepancy detection kicks in
    svc.broker_zero_debounce_observations = 1

    # Observation 1: triggers HALT
    await svc.sync_portfolio()
    assert guard.state == StormGuardState.HALT
    assert svc._halt_triggered is True

    # Observation 2 (streak=2 >= auto_correct_after=2): auto-correct fires
    await svc.sync_portfolio()

    # After auto-correct: phantom cleared, HALT resolved
    assert svc._halt_triggered is False
    assert guard.reconciliation_hold is False

    # Verify position was cleared from store
    snapshot = store.snapshot_positions()
    for _k, pos in snapshot.items():
        assert pos.symbol != "TX438500D6", "Phantom position should be cleared"


@pytest.mark.asyncio
async def test_non_platform_symbol_auto_resolved(guard):
    """Non-platform symbols with broker=0 are resolved immediately without HALT."""
    client = MagicMock()
    # Broker reports NO positions (option expired)
    client.get_positions.return_value = []
    # Platform manages TMFR1 and TXFR1 only
    client.subscribed_codes = {"TMFR1", "TXFR1", "TMFE6", "TXFE6"}
    client.alias_to_actual = {"TMFR1": "TMFE6", "TXFR1": "TXFE6"}

    store = PositionStore()
    # Seed a phantom position for a NON-platform symbol (manual trade)
    store.load_recovery(
        account_id="default",
        symbol="TX438500D6",
        net_qty=1,
        avg_price_scaled=100_0000,
        strategy_id=MANUAL_STRATEGY_ID,
    )

    svc = ReconciliationService(
        client,
        store,
        {"symbols": [{"code": "TMFR1"}, {"code": "TXFR1"}]},
        storm_guard=guard,
    )
    svc.broker_zero_debounce_observations = 1

    # Single sync should auto-resolve the non-platform phantom WITHOUT triggering HALT
    await svc.sync_portfolio()

    assert guard.state != StormGuardState.HALT, "Non-platform phantom should not trigger HALT"
    assert svc._halt_triggered is False

    # Verify phantom was cleared
    snapshot = store.snapshot_positions()
    for _k, pos in snapshot.items():
        assert pos.symbol != "TX438500D6"


class TestClearSymbolPositions:
    """PositionStore.clear_symbol_positions."""

    def test_clears_matching_positions(self):
        store = PositionStore()
        store.load_recovery(
            account_id="default",
            symbol="TX438500D6",
            net_qty=1,
            avg_price_scaled=100_0000,
            strategy_id="manual",
        )
        # Simulate merging into positions via direct assignment
        from hft_platform.execution.positions import Position

        store.positions["default:manual:TX438500D6"] = Position(
            account_id="default",
            strategy_id="manual",
            symbol="TX438500D6",
            net_qty=1,
            avg_price_scaled=100_0000,
        )
        # Also have an unrelated position
        store.positions["default:r47:TMFE6"] = Position(
            account_id="default",
            strategy_id="r47",
            symbol="TMFE6",
            net_qty=2,
            avg_price_scaled=200_0000,
        )

        cleared = store.clear_symbol_positions("TX438500D6")
        assert cleared == 1
        assert "default:manual:TX438500D6" not in store.positions
        assert "default:r47:TMFE6" in store.positions  # unrelated position preserved

    def test_clears_recovery_entries(self):
        store = PositionStore()
        store.load_recovery(
            account_id="default",
            symbol="TX438500D6",
            net_qty=1,
            avg_price_scaled=100_0000,
            strategy_id=MANUAL_STRATEGY_ID,
        )
        assert any(rd.get("symbol") == "TX438500D6" for rd in store._recovery_positions.values())

        store.clear_symbol_positions("TX438500D6")
        assert not any(rd.get("symbol") == "TX438500D6" for rd in store._recovery_positions.values())

    def test_noop_for_absent_symbol(self):
        store = PositionStore()
        cleared = store.clear_symbol_positions("NONEXISTENT")
        assert cleared == 0
