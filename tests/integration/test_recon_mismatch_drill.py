"""Reconciliation mismatch drill — verifies StormGuard HALT on position discrepancy."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.execution.positions import Position, PositionStore
from hft_platform.execution.reconciliation import (
    PositionDiscrepancy,
    ReconciliationService,
)
from hft_platform.risk.storm_guard import StormGuard, StormGuardState


@pytest.mark.asyncio
async def test_recon_small_mismatch_detected():
    """Small discrepancy (local=2, broker=0): detected but NOT critical (below threshold)."""
    store = PositionStore()
    store.positions["acct:strat:2330"] = Position(
        account_id="acct",
        strategy_id="strat",
        symbol="2330",
        net_qty=2,
        avg_price_scaled=6_000_000,
    )

    mock_client = MagicMock()
    mock_client.get_positions.return_value = []

    storm_guard = StormGuard()
    config = {"reconciliation": {"check_interval_s": 999, "grace_failures": 1}}
    recon = ReconciliationService(
        client=mock_client,
        position_store=store,
        config=config,
        storm_guard=storm_guard,
    )

    await recon.sync_portfolio()

    # Discrepancy detected
    assert len(recon._last_discrepancies) == 1
    disc = recon._last_discrepancies[0]
    assert disc.symbol == "2330"
    assert disc.local_qty == 2
    assert disc.broker_qty == 0
    assert disc.diff == 2

    # diff=2 does NOT exceed threshold max(100, 2//10)=100 → not critical
    assert not disc.is_critical
    # StormGuard stays NORMAL (no critical discrepancy)
    assert storm_guard.state == StormGuardState.NORMAL


@pytest.mark.asyncio
async def test_recon_large_mismatch_triggers_halt():
    """Large discrepancy (local=200, broker=0): critical → StormGuard HALT."""
    store = PositionStore()
    store.positions["acct:strat:2330"] = Position(
        account_id="acct",
        strategy_id="strat",
        symbol="2330",
        net_qty=200,
        avg_price_scaled=6_000_000,
    )

    mock_client = MagicMock()
    mock_client.get_positions.return_value = []

    storm_guard = StormGuard()
    assert storm_guard.state == StormGuardState.NORMAL

    config = {"reconciliation": {"check_interval_s": 999, "grace_failures": 1}}
    recon = ReconciliationService(
        client=mock_client,
        position_store=store,
        config=config,
        storm_guard=storm_guard,
    )

    await recon.sync_portfolio()

    assert len(recon._last_discrepancies) == 1
    disc = recon._last_discrepancies[0]
    assert disc.local_qty == 200
    assert disc.broker_qty == 0
    # diff=200 > max(100, 200//10=20)=100 → critical
    assert disc.is_critical

    # sync_portfolio calls _trigger_halt for critical discrepancies → HALT
    assert storm_guard.state == StormGuardState.HALT


@pytest.mark.asyncio
async def test_recon_sign_mismatch_triggers_halt():
    """Sign mismatch (local long, broker short) is always critical → HALT."""
    store = PositionStore()
    store.positions["acct:strat:2330"] = Position(
        account_id="acct",
        strategy_id="strat",
        symbol="2330",
        net_qty=5,
        avg_price_scaled=6_000_000,
    )

    # Broker reports a short position
    mock_pos = MagicMock()
    mock_pos.code = "2330"
    mock_pos.quantity = 5
    mock_pos.direction = "Action.Sell"  # negative qty

    mock_client = MagicMock()
    mock_client.get_positions.return_value = [mock_pos]

    storm_guard = StormGuard()
    config = {"reconciliation": {"check_interval_s": 999, "grace_failures": 1}}
    recon = ReconciliationService(
        client=mock_client,
        position_store=store,
        config=config,
        storm_guard=storm_guard,
    )

    await recon.sync_portfolio()

    assert len(recon._last_discrepancies) == 1
    disc = recon._last_discrepancies[0]
    assert disc.local_qty == 5
    assert disc.broker_qty == -5
    assert disc.is_critical  # sign mismatch always critical

    assert storm_guard.state == StormGuardState.HALT


def test_position_discrepancy_sign_mismatch_is_critical():
    """Unit: sign mismatch (local long, broker short) is always critical."""
    disc = PositionDiscrepancy(symbol="2330", local_qty=5, broker_qty=-5, diff=10)
    assert disc.is_critical
    assert disc.severity == "critical"


@pytest.mark.asyncio
async def test_recon_no_discrepancy_stays_normal():
    """When local and broker agree, no discrepancy and StormGuard stays NORMAL."""
    store = PositionStore()
    store.positions["acct:strat:2330"] = Position(
        account_id="acct",
        strategy_id="strat",
        symbol="2330",
        net_qty=3,
        avg_price_scaled=6_000_000,
    )

    mock_pos = MagicMock()
    mock_pos.code = "2330"
    mock_pos.quantity = 3
    mock_pos.direction = "Action.Buy"

    mock_client = MagicMock()
    mock_client.get_positions.return_value = [mock_pos]

    storm_guard = StormGuard()
    config = {"reconciliation": {"check_interval_s": 999, "grace_failures": 1}}
    recon = ReconciliationService(
        client=mock_client,
        position_store=store,
        config=config,
        storm_guard=storm_guard,
    )

    await recon.sync_portfolio()

    assert len(recon._last_discrepancies) == 0
    assert storm_guard.state == StormGuardState.NORMAL
