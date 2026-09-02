"""Reconciliation must never delete a position the platform itself opened.

Regression cover for the 2026-09-02 defect: ``sync_portfolio``'s
non-platform auto-resolve cleared R47_MAKER_TMF's live TMFI6 position 19 times
in one session. Three independent conditions lined up, and each test below
pins one of them:

1. the platform symbol universe was read from the ORDER client, which carries
   neither ``subscribed_codes`` nor ``alias_to_actual``, so it degraded to the
   configured list -- under loop_v1 the continuous front-month *aliases*
   ``{TMFR1, TXFR1}``, which no tradable contract code can ever match;
2. ``HFT_ORDER_MODE=sim`` routes orders to a paper venue while
   ``get_positions()`` reads the real account, so every platform position
   reads back as ``broker_qty == 0`` -- the exact shape auto-resolve deletes;
3. ``reconciliation_discrepancy_count`` was set from the post-resolution list,
   so a cycle that deleted a position reported zero discrepancies.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.constants import MANUAL_STRATEGY_ID
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.risk.storm_guard import StormGuard, StormGuardState

_STRATEGY = "R47_MAKER_TMF"
_TRADED = "TMFI6"  # resolved front-month contract that actually trades
_ALIAS = "TMFR1"  # continuous alias the loop config declares


@pytest.fixture
def guard():
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        g = StormGuard()
        g._halt_cooldown_s = 0.0
        g._storm_cooldown_s = 0.0
        g._de_escalate_threshold = 1
        yield g


def _store_with_strategy_position(symbol: str = _TRADED, strategy_id: str = _STRATEGY) -> PositionStore:
    store = PositionStore()
    store.load_recovery(
        account_id="default",
        symbol=symbol,
        net_qty=-1,
        avg_price_scaled=462_390_000,
        strategy_id=strategy_id,
    )
    return store


def _service(store: PositionStore, guard: StormGuard, **kwargs) -> ReconciliationService:
    client = MagicMock()
    client.get_positions.return_value = []  # broker reports nothing
    # The ORDER client legitimately has neither attribute.
    del client.subscribed_codes
    del client.alias_to_actual
    svc = ReconciliationService(
        client,
        store,
        {"symbols": [{"code": _ALIAS}, {"code": "TXFR1"}]},
        storm_guard=guard,
        order_mode=kwargs.pop("order_mode", "live"),
        **kwargs,
    )
    svc.broker_zero_debounce_observations = 1
    return svc


def _symbols_in(store: PositionStore) -> set[str]:
    return {pos.symbol for pos in store.snapshot_positions().values()} | {
        str((data or {}).get("symbol") or "") for data in (getattr(store, "_recovery_positions", {}) or {}).values()
    }


@pytest.mark.asyncio
async def test_strategy_owned_position_survives_broker_zero_snapshot(guard):
    """A position attributed to a named strategy is never auto-resolved."""
    store = _store_with_strategy_position()
    svc = _service(store, guard)

    await svc.sync_portfolio()

    assert _TRADED in _symbols_in(store), "reconciliation deleted the platform's own position"


@pytest.mark.asyncio
async def test_resolved_contract_protected_by_quote_side_alias_map(guard):
    """The alias map resolves TMFR1 -> TMFI6, so the traded code is in-universe.

    This is the guard that still holds when attribution is unavailable, i.e.
    when the position carries no usable strategy_id.
    """
    store = _store_with_strategy_position(strategy_id="UNKNOWN")
    symbol_source = SimpleNamespace(
        subscribed_codes={_ALIAS, "TXFR1"},
        alias_to_actual={_ALIAS: _TRADED, "TXFR1": "TXFI6"},
    )
    svc = _service(store, guard, symbol_source=symbol_source)

    await svc.sync_portfolio()

    assert _TRADED in _symbols_in(store), "resolved contract was treated as a non-platform phantom"


@pytest.mark.asyncio
async def test_manual_position_is_still_auto_resolved(guard):
    """MANUAL marks an externally placed position -- auto-resolve must still clear it."""
    store = _store_with_strategy_position(symbol="TX438500D6", strategy_id=MANUAL_STRATEGY_ID)
    svc = _service(store, guard)

    await svc.sync_portfolio()

    assert "TX438500D6" not in _symbols_in(store), "manual phantom should still be auto-resolved"
    assert guard.state != StormGuardState.HALT


@pytest.mark.asyncio
async def test_discrepancy_gauge_counts_before_auto_resolution(guard):
    """A cycle that deletes a position must not report zero discrepancies."""
    store = _store_with_strategy_position(symbol="TX438500D6", strategy_id=MANUAL_STRATEGY_ID)
    svc = _service(store, guard)
    metrics = MagicMock()
    with patch.object(ReconciliationService, "_metrics", staticmethod(lambda: metrics)):
        await svc.sync_portfolio()

    observed = [call.args[0] for call in metrics.reconciliation_discrepancy_count.set.call_args_list]
    assert observed, "discrepancy gauge was never set"
    assert max(observed) >= 1, f"auto-resolved discrepancy was hidden from the gauge: {observed}"


@pytest.mark.asyncio
async def test_sim_order_mode_records_not_comparable_and_deletes_nothing(guard):
    """Under sim routing the broker cannot confirm platform positions."""
    store = _store_with_strategy_position(symbol="TX438500D6", strategy_id=MANUAL_STRATEGY_ID)
    svc = _service(store, guard, order_mode="sim")
    metrics = MagicMock()
    with patch.object(ReconciliationService, "_metrics", staticmethod(lambda: metrics)):
        await svc.sync_portfolio()

    results = [call.kwargs.get("result") for call in metrics.reconciliation_sync_total.labels.call_args_list]
    assert "not_comparable" in results, f"sim cycle reported {results}, not not_comparable"
    assert "success" not in results, "a cycle that verified nothing reported success"
    assert "TX438500D6" in _symbols_in(store), "sim cycle deleted a position it could not verify"
    metrics.reconciliation_not_comparable.set.assert_called_with(1)
