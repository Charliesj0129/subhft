"""Tests for M9: Per-strategy reconciliation drift logging.

Covers:
- Per-strategy breakdown logged at DEBUG level during every sync
- Strategies contributing to drifting symbols logged at WARNING level when discrepancies exist
- No attribution warning logged when there are no discrepancies
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.positions import Position, PositionStore
from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.risk.storm_guard import StormGuard


def _make_store_with_positions(entries: list[tuple[str, str, str, int]]) -> PositionStore:
    """Build a PositionStore pre-populated with positions.

    ``entries`` is a list of (account_id, strategy_id, symbol, net_qty).
    """
    store = PositionStore()
    for account_id, strategy_id, symbol, net_qty in entries:
        key = f"{account_id}:{strategy_id}:{symbol}"
        pos = Position(account_id=account_id, strategy_id=strategy_id, symbol=symbol)
        pos.net_qty = net_qty
        store.positions[key] = pos
    return store


def _make_service(client, store) -> ReconciliationService:
    # broker_zero_debounce_observations=1 so broker-empty snapshots aren't silently
    # swallowed on first observation (default=2 would hide the discrepancy).
    return ReconciliationService(
        client, store, {"reconciliation": {"broker_zero_debounce_observations": 1}}, storm_guard=StormGuard()
    )


# ---------------------------------------------------------------------------
# DEBUG breakdown is always logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_strategy_breakdown_logged_no_discrepancy():
    """Per-strategy breakdown is logged at DEBUG when broker matches.

    Downgraded from INFO (was 5 s cadence → 17 k lines/day). When discrepancies
    exist the drift attribution still fires at WARNING (see below).
    """
    client = MagicMock()
    # Broker reports 10 of 2330
    client.get_positions.return_value = [SimpleNamespace(code="2330", quantity=10, direction="Action.Buy")]

    store = _make_store_with_positions([("acc1", "strat_a", "2330", 10)])
    service = _make_service(client, store)

    with patch("hft_platform.execution.reconciliation.logger") as mock_logger:
        await service.sync_portfolio()

    debug_messages = [c[0][0] for c in mock_logger.debug.call_args_list]
    assert "Portfolio Sync: Per-strategy position breakdown" in debug_messages

    # Verify the breakdown content has the right strategy
    debug_kwargs = {c[0][0]: c[1] for c in mock_logger.debug.call_args_list if c[0]}
    breakdown_kwargs = debug_kwargs.get("Portfolio Sync: Per-strategy position breakdown", {})
    assert "strat_a" in breakdown_kwargs.get("strategies", [])


@pytest.mark.asyncio
async def test_per_strategy_breakdown_contains_all_strategies():
    """Breakdown includes all strategies when multiple strategies hold positions."""
    client = MagicMock()
    client.get_positions.return_value = [SimpleNamespace(code="2330", quantity=20, direction="Action.Buy")]

    store = _make_store_with_positions(
        [
            ("acc1", "strat_a", "2330", 12),
            ("acc1", "strat_b", "2330", 8),
        ]
    )
    service = _make_service(client, store)

    with patch("hft_platform.execution.reconciliation.logger") as mock_logger:
        await service.sync_portfolio()

    debug_calls = [
        c for c in mock_logger.debug.call_args_list if c[0][0] == "Portfolio Sync: Per-strategy position breakdown"
    ]
    assert debug_calls, "Expected per-strategy breakdown DEBUG log"
    strategies = debug_calls[0][1].get("strategies", [])
    assert "strat_a" in strategies
    assert "strat_b" in strategies


# ---------------------------------------------------------------------------
# WARNING attribution when discrepancies exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_attribution_logged_at_warning_when_discrepancy():
    """When discrepancies exist, per-strategy attribution is logged at WARNING."""
    client = MagicMock()
    # Broker: 2330=5, local: 2330=10 (strat_a=7, strat_b=3)
    client.get_positions.return_value = [SimpleNamespace(code="2330", quantity=5, direction="Action.Buy")]

    store = _make_store_with_positions(
        [
            ("acc1", "strat_a", "2330", 7),
            ("acc1", "strat_b", "2330", 3),
        ]
    )
    service = _make_service(client, store)

    with patch("hft_platform.execution.reconciliation.logger") as mock_logger:
        await service.sync_portfolio()

    warning_messages = [c[0][0] for c in mock_logger.warning.call_args_list]
    assert "Per-strategy drift attribution" in warning_messages, (
        f"Expected drift attribution warning, got: {warning_messages}"
    )

    # Validate attribution content includes drifting symbol
    drift_call = next(c for c in mock_logger.warning.call_args_list if c[0][0] == "Per-strategy drift attribution")
    assert "2330" in drift_call[1].get("drifting_symbols", [])


@pytest.mark.asyncio
async def test_drift_attribution_identifies_contributing_strategies():
    """Attribution includes the specific strategies contributing to drifting symbol."""
    client = MagicMock()
    # Broker: 2330=0, local: 2330=15 (strat_mm=10, strat_arb=5)
    client.get_positions.return_value = []

    store = _make_store_with_positions(
        [
            ("acc1", "strat_mm", "2330", 10),
            ("acc1", "strat_arb", "2330", 5),
        ]
    )
    service = _make_service(client, store)

    with patch("hft_platform.execution.reconciliation.logger") as mock_logger:
        await service.sync_portfolio()

    drift_call = next(
        (c for c in mock_logger.warning.call_args_list if c[0][0] == "Per-strategy drift attribution"),
        None,
    )
    assert drift_call is not None, "Expected per-strategy drift attribution WARNING"
    attribution = drift_call[1].get("attribution", {})
    # Both strategies should appear under the drifting symbol
    assert "2330" in attribution
    assert "strat_mm" in attribution["2330"]
    assert "strat_arb" in attribution["2330"]
    assert attribution["2330"]["strat_mm"] == 10
    assert attribution["2330"]["strat_arb"] == 5


# ---------------------------------------------------------------------------
# No drift attribution warning when positions match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_attribution_warning_when_no_discrepancy():
    """Per-strategy attribution warning is NOT emitted when positions match broker."""
    client = MagicMock()
    client.get_positions.return_value = [SimpleNamespace(code="2330", quantity=10, direction="Action.Buy")]

    store = _make_store_with_positions([("acc1", "strat_a", "2330", 10)])
    service = _make_service(client, store)

    with patch("hft_platform.execution.reconciliation.logger") as mock_logger:
        await service.sync_portfolio()

    warning_messages = [c[0][0] for c in mock_logger.warning.call_args_list]
    assert "Per-strategy drift attribution" not in warning_messages


# ---------------------------------------------------------------------------
# Only strategies with non-zero qty appear in attribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attribution_excludes_zero_position_strategies():
    """Strategies with zero net_qty for a drifting symbol are excluded from attribution."""
    client = MagicMock()
    # Broker: 2330=0; local: 2330=5 (strat_a=5, strat_b=0)
    client.get_positions.return_value = []

    store = _make_store_with_positions(
        [
            ("acc1", "strat_a", "2330", 5),
            ("acc1", "strat_b", "2330", 0),
        ]
    )
    service = _make_service(client, store)

    with patch("hft_platform.execution.reconciliation.logger") as mock_logger:
        await service.sync_portfolio()

    drift_call = next(
        (c for c in mock_logger.warning.call_args_list if c[0][0] == "Per-strategy drift attribution"),
        None,
    )
    assert drift_call is not None
    attribution = drift_call[1].get("attribution", {})
    assert "2330" in attribution
    # strat_b has qty=0, should NOT appear
    assert "strat_b" not in attribution.get("2330", {})
    assert "strat_a" in attribution.get("2330", {})
