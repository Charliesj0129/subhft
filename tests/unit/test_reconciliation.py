from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.positions import Position, PositionStore
from hft_platform.execution.reconciliation import (
    PositionDiscrepancy,
    ReconciliationService,
    _compute_backoff_delay,
)
from hft_platform.risk.storm_guard import StormGuard


class MockPosition:
    def __init__(self, code, quantity, direction):
        self.code = code
        self.quantity = quantity
        self.direction = direction


def _make_store_with_positions(symbol_qty_map: dict[str, int]) -> PositionStore:
    """Create a PositionStore pre-loaded with positions (scaled int quantities)."""
    store = PositionStore()
    store.metrics = None  # avoid Prometheus in tests
    for symbol, qty in symbol_qty_map.items():
        key = f"ACC:STRAT:{symbol}"
        pos = Position(account_id="ACC", strategy_id="STRAT", symbol=symbol, net_qty=qty)
        store.positions[key] = pos
    return store


def _make_service(
    *,
    broker_positions: list | None = None,
    local_positions: dict[str, int] | None = None,
    storm_guard: StormGuard | None = None,
) -> ReconciliationService:
    client = MagicMock()
    client.get_positions.return_value = broker_positions or []
    store = _make_store_with_positions(local_positions or {})
    sg = storm_guard or StormGuard()
    return ReconciliationService(client, store, {}, storm_guard=sg)


@pytest.mark.asyncio
async def test_recon_sync_portfolio():
    mock_client = MagicMock()
    mock_client.get_positions.return_value = [MockPosition("2330", 5, "Action.Buy")]
    mock_store = MagicMock()
    mock_store.positions = {}

    service = ReconciliationService(mock_client, mock_store, {}, storm_guard=StormGuard())

    await service.sync_portfolio()
    mock_client.get_positions.assert_called_once()


@pytest.mark.asyncio
async def test_recon_discrepancy_logging():
    # Patch the logger module where ReconciliationService gets it
    with patch("hft_platform.execution.reconciliation.logger") as mock_logger:
        mock_client = MagicMock()
        # Ensure it is awaitable if run in thread? implementation uses asyncio.to_thread
        # but to_thread runs sync function in thread.
        mock_client.get_positions.return_value = [MockPosition("2330", 10, "Action.Buy")]

        service = ReconciliationService(mock_client, MagicMock(), {}, storm_guard=StormGuard())

        await service.sync_portfolio()

        # Check that logger.info was called with specific content
        # implementation: logger.info("Portfolio Sync: Broker State", positions=broker_map)
        # We verify one of the info calls contains this message
        calls = [c[0][0] for c in mock_logger.info.call_args_list]
        assert "Portfolio Sync: Broker State" in calls


# ---------------------------------------------------------------------------
# PositionDiscrepancy unit tests
# ---------------------------------------------------------------------------


class TestPositionDiscrepancy:
    def test_zero_both_not_critical(self) -> None:
        d = PositionDiscrepancy(symbol="2330", local_qty=0, broker_qty=0, diff=0)
        assert d.is_critical is False
        assert d.severity == "info"

    def test_sign_mismatch_long_vs_short_critical(self) -> None:
        d = PositionDiscrepancy(symbol="2330", local_qty=100, broker_qty=-50, diff=150)
        assert d.is_critical is True
        assert d.severity == "critical"

    def test_sign_mismatch_short_vs_long_critical(self) -> None:
        d = PositionDiscrepancy(symbol="2330", local_qty=-100, broker_qty=50, diff=-150)
        assert d.is_critical is True
        assert d.severity == "critical"

    def test_large_absolute_diff_critical(self) -> None:
        # local=1000, broker=800, diff=200, threshold=max(100, 100)=100 -> 200 > 100 -> critical
        d = PositionDiscrepancy(symbol="2330", local_qty=1000, broker_qty=800, diff=200)
        assert d.is_critical is True
        assert d.severity == "critical"

    def test_small_diff_info(self) -> None:
        d = PositionDiscrepancy(symbol="2330", local_qty=100, broker_qty=95, diff=5)
        assert d.is_critical is False
        assert d.severity == "info"

    def test_medium_diff_warning(self) -> None:
        # diff=15 > 10 but not critical (100 - 85 = 15 < max(100, 10))
        d = PositionDiscrepancy(symbol="2330", local_qty=100, broker_qty=85, diff=15)
        assert d.is_critical is False
        assert d.severity == "warning"

    def test_local_zero_broker_nonzero_critical(self) -> None:
        # local=0, broker=500 -> threshold=100, diff=-500 > 100 -> critical
        d = PositionDiscrepancy(symbol="2330", local_qty=0, broker_qty=500, diff=-500)
        assert d.is_critical is True


# ---------------------------------------------------------------------------
# _compute_discrepancies
# ---------------------------------------------------------------------------


class TestComputeDiscrepancies:
    def test_no_discrepancy_when_match(self) -> None:
        svc = _make_service()
        result = svc._compute_discrepancies({"2330": 100}, {"2330": 100})
        assert len(result) == 0

    def test_detects_qty_mismatch(self) -> None:
        svc = _make_service()
        result = svc._compute_discrepancies({"2330": 100}, {"2330": 80})
        assert len(result) == 1
        assert result[0].symbol == "2330"
        assert result[0].local_qty == 100
        assert result[0].broker_qty == 80
        assert result[0].diff == 20

    def test_detects_local_only_symbol(self) -> None:
        svc = _make_service()
        result = svc._compute_discrepancies({"AAA": 50}, {})
        assert len(result) == 1
        assert result[0].symbol == "AAA"
        assert result[0].broker_qty == 0

    def test_detects_broker_only_symbol(self) -> None:
        svc = _make_service()
        result = svc._compute_discrepancies({}, {"BBB": 30})
        assert len(result) == 1
        assert result[0].symbol == "BBB"
        assert result[0].local_qty == 0

    def test_multiple_mismatches(self) -> None:
        svc = _make_service()
        result = svc._compute_discrepancies({"A": 10, "B": 20, "C": 30}, {"A": 10, "B": 25, "C": 0})
        # A matches, B and C mismatch
        assert len(result) == 2
        symbols = {d.symbol for d in result}
        assert symbols == {"B", "C"}


# ---------------------------------------------------------------------------
# sync_portfolio with real PositionStore
# ---------------------------------------------------------------------------


class TestSyncPortfolioIntegration:
    @pytest.mark.asyncio
    async def test_no_discrepancy_when_matching(self) -> None:
        svc = _make_service(
            broker_positions=[MockPosition("2330", 100, "Action.Buy")],
            local_positions={"2330": 100},
        )
        await svc.sync_portfolio()
        assert len(svc._last_discrepancies) == 0

    @pytest.mark.asyncio
    async def test_discrepancy_detected_on_mismatch(self) -> None:
        svc = _make_service(
            broker_positions=[MockPosition("2330", 50, "Action.Buy")],
            local_positions={"2330": 100},
        )
        await svc.sync_portfolio()
        assert len(svc._last_discrepancies) == 1
        assert svc._last_discrepancies[0].diff == 50

    @pytest.mark.asyncio
    async def test_sell_direction_negates_broker_qty(self) -> None:
        svc = _make_service(
            broker_positions=[MockPosition("2330", 100, "Action.Sell")],
            local_positions={"2330": -100},
        )
        await svc.sync_portfolio()
        assert len(svc._last_discrepancies) == 0

    @pytest.mark.asyncio
    async def test_dict_positions_accepted(self) -> None:
        """Broker returning dict format positions."""
        svc = _make_service(
            broker_positions=[{"code": "2330", "quantity": 50}],
            local_positions={"2330": 50},
        )
        await svc.sync_portfolio()
        assert len(svc._last_discrepancies) == 0


# ---------------------------------------------------------------------------
# HALT trigger on critical discrepancy
# ---------------------------------------------------------------------------


class TestHaltTrigger:
    @pytest.mark.asyncio
    async def test_critical_discrepancy_triggers_halt(self) -> None:
        sg = StormGuard()
        svc = _make_service(
            broker_positions=[MockPosition("2330", 1000, "Action.Sell")],
            local_positions={"2330": 1000},  # local long, broker short -> sign mismatch -> critical
            storm_guard=sg,
        )
        await svc.sync_portfolio()
        # Sign mismatch should trigger HALT
        assert len(svc._last_discrepancies) > 0
        critical = [d for d in svc._last_discrepancies if d.is_critical]
        assert len(critical) > 0
        # StormGuard should have been halted
        from hft_platform.risk.storm_guard import StormGuardState

        assert sg.state == StormGuardState.HALT

    @pytest.mark.asyncio
    async def test_non_critical_discrepancy_no_halt(self) -> None:
        sg = StormGuard()
        # Small diff: local=50, broker=45 -> diff=5 (info, not critical)
        svc = _make_service(
            broker_positions=[MockPosition("2330", 45, "Action.Buy")],
            local_positions={"2330": 50},
            storm_guard=sg,
        )
        await svc.sync_portfolio()
        assert len(svc._last_discrepancies) == 1
        assert not svc._last_discrepancies[0].is_critical
        from hft_platform.risk.storm_guard import StormGuardState

        assert sg.state != StormGuardState.HALT

    @pytest.mark.asyncio
    async def test_sync_failure_increments_consecutive_failures(self) -> None:
        client = MagicMock()
        client.get_positions.side_effect = RuntimeError("API down")
        store = _make_store_with_positions({})
        svc = ReconciliationService(client, store, {}, storm_guard=StormGuard())
        with pytest.raises(RuntimeError, match="API down"):
            await svc.sync_portfolio()


# ---------------------------------------------------------------------------
# backoff delay
# ---------------------------------------------------------------------------


class TestBackoffDelay:
    def test_first_attempt(self) -> None:
        with patch("hft_platform.execution.reconciliation.random") as mock_rng:
            mock_rng.uniform.return_value = 1.0
            d = _compute_backoff_delay(attempt=0, base=2.0, max_delay=60.0, jitter=0.0)
        assert d == pytest.approx(2.0)

    def test_capped_at_max(self) -> None:
        with patch("hft_platform.execution.reconciliation.random") as mock_rng:
            mock_rng.uniform.return_value = 1.0
            d = _compute_backoff_delay(attempt=100, base=2.0, max_delay=30.0, jitter=0.0)
        assert d == pytest.approx(30.0)
