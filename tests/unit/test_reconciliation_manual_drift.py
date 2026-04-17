"""Regression tests for Bug 14: MANUAL drift auto-correct.

Scenario: A user manually closes a futures position via the broker app
while the engine still holds a MANUAL-attributed entry for the same
symbol (typically with ``avg_price_scaled=-1`` sentinel from a prior
reconciliation recovery). The engine's local ``positions`` dict must
eventually be cleared without requiring a restart.

These tests verify:

1. ``clear_symbol_positions`` can be scoped to a specific ``strategy_id``
   so active strategy positions on the same symbol are not wiped.
2. The reconciliation auto-correct path removes MANUAL-only drift
   without requiring the operator to restart the engine.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.constants import MANUAL_STRATEGY_ID
from hft_platform.execution.positions import Position, PositionStore
from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.risk.storm_guard import StormGuard


@pytest.fixture
def guard():
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        g = StormGuard()
        g._halt_cooldown_s = 0.0
        g._storm_cooldown_s = 0.0
        g._de_escalate_threshold = 1
        yield g


def _inject_manual_short(store: PositionStore, symbol: str = "TMFE6") -> None:
    """Inject a MANUAL short position directly (simulating prior recovery)."""
    key = f"default:{MANUAL_STRATEGY_ID}:{symbol}"
    store.positions[key] = Position(
        account_id="default",
        strategy_id=MANUAL_STRATEGY_ID,
        symbol=symbol,
        net_qty=-1,
        avg_price_scaled=-1,  # sentinel: unknown cost basis
    )


def test_clear_symbol_positions_strategy_scoped_preserves_active_strategy():
    """Strategy-scoped clear must not remove other strategies' positions."""
    store = PositionStore()
    symbol = "TMFE6"
    # MANUAL phantom
    manual_key = f"default:{MANUAL_STRATEGY_ID}:{symbol}"
    store.positions[manual_key] = Position(
        account_id="default",
        strategy_id=MANUAL_STRATEGY_ID,
        symbol=symbol,
        net_qty=-1,
        avg_price_scaled=-1,
    )
    # Active strategy holding same symbol
    strat_key = f"default:R47:{symbol}"
    store.positions[strat_key] = Position(
        account_id="default",
        strategy_id="R47",
        symbol=symbol,
        net_qty=2,
        avg_price_scaled=1_234_000,
    )

    removed = store.clear_symbol_positions(symbol, strategy_id=MANUAL_STRATEGY_ID)

    assert removed == 1
    assert manual_key not in store.positions
    assert strat_key in store.positions, "R47 active position must not be cleared"
    assert store.positions[strat_key].net_qty == 2


def test_clear_symbol_positions_default_clears_all_strategies_for_symbol():
    """Without a strategy_id filter, behavior is unchanged (clears all)."""
    store = PositionStore()
    symbol = "TMFE6"
    store.positions[f"default:{MANUAL_STRATEGY_ID}:{symbol}"] = Position(
        account_id="default",
        strategy_id=MANUAL_STRATEGY_ID,
        symbol=symbol,
        net_qty=-1,
        avg_price_scaled=-1,
    )
    store.positions[f"default:R47:{symbol}"] = Position(
        account_id="default",
        strategy_id="R47",
        symbol=symbol,
        net_qty=2,
        avg_price_scaled=1_234_000,
    )

    removed = store.clear_symbol_positions(symbol)

    assert removed == 2
    assert not any(pos.symbol == symbol for pos in store.positions.values())


@pytest.mark.asyncio
async def test_auto_correct_removes_manual_phantom_without_restart(guard):
    """End-to-end: MANUAL -1 local + broker flat should self-heal via auto-correct."""
    client = MagicMock()
    # Broker reports no TMFE6 position (user closed it manually).
    client.get_positions.return_value = []
    # Non-empty subscribed set so TMFE6 IS considered a platform symbol,
    # forcing the drift into the HALT/auto-correct path rather than the
    # non-platform phantom auto-resolve branch.
    client.subscribed_codes = {"TMFE6"}

    store = PositionStore()
    _inject_manual_short(store, "TMFE6")

    svc = ReconciliationService(
        client,
        store,
        {
            "reconciliation": {
                "check_interval_s": 0.01,
                "broker_zero_debounce_observations": 1,
            },
            "symbols": [{"code": "TMFE6"}],
        },
        storm_guard=guard,
    )
    svc._auto_correct_after = 2
    svc._critical_drift_debounce = 1

    # First sync: crosses broker_zero debounce (=1) and triggers HALT path.
    await svc.sync_portfolio()
    # Second sync: streak hits auto-correct threshold and clears MANUAL entry.
    await svc.sync_portfolio()

    manual_key = f"default:{MANUAL_STRATEGY_ID}:TMFE6"
    assert manual_key not in store.positions, (
        "MANUAL phantom position must be cleared by auto-correct without restart"
    )
    # Reconciliation state should also reset.
    assert svc._halt_triggered is False
    assert svc._critical_drift_streak == 0
