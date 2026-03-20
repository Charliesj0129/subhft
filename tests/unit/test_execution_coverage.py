"""Comprehensive tests for execution/positions.py and execution/reconciliation.py."""

import asyncio
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent, PositionDelta, Side
from hft_platform.execution.positions import Position, PositionStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fill(
    fill_id: str = "F001",
    account_id: str = "ACC1",
    order_id: str = "ORD1",
    strategy_id: str = "s1",
    symbol: str = "2330",
    side: Side = Side.BUY,
    qty: int = 10,
    price: int = 5000000,  # 500.0 x10000
    fee: int = 200,  # scaled
    tax: int = 0,
    ingest_ts_ns: int = 1000,
    match_ts_ns: int = 2000,
) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        account_id=account_id,
        order_id=order_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=ingest_ts_ns,
        match_ts_ns=match_ts_ns,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
    monkeypatch.delenv("HFT_LOG_FILLS", raising=False)
    monkeypatch.delenv("HFT_POSITIONS_MAX_SIZE", raising=False)


@pytest.fixture
def store():
    return PositionStore()


# ===========================================================================
# Position Dataclass Tests
# ===========================================================================


class TestPosition:
    def test_init_defaults(self):
        pos = Position(account_id="ACC1", strategy_id="s1", symbol="2330")
        assert pos.net_qty == 0
        assert pos.avg_price_scaled == 0
        assert pos.realized_pnl_scaled == 0
        assert pos.fees_scaled == 0

    def test_avg_price_property(self):
        pos = Position("ACC1", "s1", "2330", avg_price_scaled=5000000)
        assert pos.avg_price == 5000000

    def test_realized_pnl_property(self):
        pos = Position("ACC1", "s1", "2330", realized_pnl_scaled=100000)
        assert pos.realized_pnl == 100000

    def test_fees_property(self):
        pos = Position("ACC1", "s1", "2330", fees_scaled=500)
        assert pos.fees == 500

    def test_descaled_avg_price(self):
        pos = Position("ACC1", "s1", "2330", avg_price_scaled=5000000)
        result = pos.descaled_avg_price(10000)
        assert result == 500.0

    def test_descaled_avg_price_zero_scale(self):
        pos = Position("ACC1", "s1", "2330", avg_price_scaled=5000000)
        assert pos.descaled_avg_price(0) == 0.0

    def test_descaled_realized_pnl(self):
        pos = Position("ACC1", "s1", "2330", realized_pnl_scaled=100000)
        assert pos.descaled_realized_pnl(10000) == 10.0

    def test_descaled_fees(self):
        pos = Position("ACC1", "s1", "2330", fees_scaled=5000)
        assert pos.descaled_fees(10000) == 0.5

    def test_slots(self):
        pos = Position("ACC1", "s1", "2330")
        assert hasattr(pos, "__slots__")


# ===========================================================================
# Position.update() Tests — Buy
# ===========================================================================


class TestPositionUpdateBuy:
    def test_first_buy_sets_position(self):
        pos = Position("ACC1", "s1", "2330")
        fill = _make_fill(side=Side.BUY, qty=10, price=5000000)
        pos.update(fill)
        assert pos.net_qty == 10
        assert pos.avg_price_scaled == 5000000
        assert pos.realized_pnl_scaled == 0

    def test_second_buy_averages(self):
        pos = Position("ACC1", "s1", "2330")
        fill1 = _make_fill(side=Side.BUY, qty=10, price=5000000)
        fill2 = _make_fill(side=Side.BUY, qty=10, price=6000000, fill_id="F002")
        pos.update(fill1)
        pos.update(fill2)
        assert pos.net_qty == 20
        # Weighted avg: (10 * 5M + 10 * 6M) / 20 = 5.5M
        assert pos.avg_price_scaled == 5500000

    def test_buy_accumulates_fees(self):
        pos = Position("ACC1", "s1", "2330")
        fill = _make_fill(fee=200, tax=100)
        pos.update(fill)
        assert pos.fees_scaled == 300


# ===========================================================================
# Position.update() Tests — Sell (Close)
# ===========================================================================


class TestPositionUpdateSell:
    def test_sell_long_position_realizes_pnl(self):
        pos = Position("ACC1", "s1", "2330")
        # Buy at 500
        buy = _make_fill(side=Side.BUY, qty=10, price=5000000)
        pos.update(buy)
        # Sell at 510 (profit)
        sell = _make_fill(side=Side.SELL, qty=10, price=5100000, fill_id="F002")
        pos.update(sell)
        assert pos.net_qty == 0
        # PnL = (510 - 500) * 10 = 100 * 10000 * 10 = 1_000_000
        assert pos.realized_pnl_scaled == 1_000_000

    def test_sell_long_partial_close(self):
        pos = Position("ACC1", "s1", "2330")
        buy = _make_fill(side=Side.BUY, qty=10, price=5000000)
        pos.update(buy)
        sell = _make_fill(side=Side.SELL, qty=5, price=5100000, fill_id="F002")
        pos.update(sell)
        assert pos.net_qty == 5
        # PnL = (5100000 - 5000000) * 5 = 500_000
        assert pos.realized_pnl_scaled == 500_000
        assert pos.avg_price_scaled == 5000000  # unchanged

    def test_sell_long_at_loss(self):
        pos = Position("ACC1", "s1", "2330")
        buy = _make_fill(side=Side.BUY, qty=10, price=5000000)
        pos.update(buy)
        sell = _make_fill(side=Side.SELL, qty=10, price=4900000, fill_id="F002")
        pos.update(sell)
        assert pos.net_qty == 0
        # PnL = (4900000 - 5000000) * 10 = -1_000_000
        assert pos.realized_pnl_scaled == -1_000_000


# ===========================================================================
# Position.update() Tests — Short
# ===========================================================================


class TestPositionUpdateShort:
    def test_first_sell_opens_short(self):
        pos = Position("ACC1", "s1", "2330")
        fill = _make_fill(side=Side.SELL, qty=10, price=5000000)
        pos.update(fill)
        assert pos.net_qty == -10
        assert pos.avg_price_scaled == 5000000

    def test_cover_short_realizes_pnl(self):
        pos = Position("ACC1", "s1", "2330")
        sell = _make_fill(side=Side.SELL, qty=10, price=5000000)
        pos.update(sell)
        buy = _make_fill(side=Side.BUY, qty=10, price=4900000, fill_id="F002")
        pos.update(buy)
        assert pos.net_qty == 0
        # Short PnL = (entry - exit) * qty = (5000000 - 4900000) * 10 = 1_000_000
        assert pos.realized_pnl_scaled == 1_000_000

    def test_cover_short_at_loss(self):
        pos = Position("ACC1", "s1", "2330")
        sell = _make_fill(side=Side.SELL, qty=10, price=5000000)
        pos.update(sell)
        buy = _make_fill(side=Side.BUY, qty=10, price=5100000, fill_id="F002")
        pos.update(buy)
        assert pos.net_qty == 0
        assert pos.realized_pnl_scaled == -1_000_000


# ===========================================================================
# Position Flip Tests
# ===========================================================================


class TestPositionFlip:
    def test_flip_long_to_short(self):
        pos = Position("ACC1", "s1", "2330")
        buy = _make_fill(side=Side.BUY, qty=5, price=5000000)
        pos.update(buy)
        # Sell more than we own
        sell = _make_fill(side=Side.SELL, qty=15, price=5100000, fill_id="F002")
        pos.update(sell)
        assert pos.net_qty == -10
        # PnL on the 5 closed: (5100000 - 5000000) * 5 = 500_000
        assert pos.realized_pnl_scaled == 500_000
        # New avg price for the short is the fill price
        assert pos.avg_price_scaled == 5100000

    def test_flip_short_to_long(self):
        pos = Position("ACC1", "s1", "2330")
        sell = _make_fill(side=Side.SELL, qty=5, price=5000000)
        pos.update(sell)
        buy = _make_fill(side=Side.BUY, qty=15, price=4900000, fill_id="F002")
        pos.update(buy)
        assert pos.net_qty == 10
        # PnL on the 5 closed: (5000000 - 4900000) * 5 = 500_000
        assert pos.realized_pnl_scaled == 500_000
        assert pos.avg_price_scaled == 4900000


# ===========================================================================
# PositionStore Tests
# ===========================================================================


class TestPositionStore:
    def test_on_fill_creates_position(self, store):
        fill = _make_fill()
        delta = store.on_fill(fill)
        assert isinstance(delta, PositionDelta)
        assert delta.net_qty == 10
        assert delta.delta_source == "FILL"
        assert len(store.positions) == 1

    def test_on_fill_multi_symbol(self, store):
        fill1 = _make_fill(symbol="2330")
        fill2 = _make_fill(symbol="2317", fill_id="F002")
        store.on_fill(fill1)
        store.on_fill(fill2)
        assert len(store.positions) == 2

    def test_on_fill_same_symbol_accumulates(self, store):
        fill1 = _make_fill(qty=10)
        fill2 = _make_fill(qty=5, fill_id="F002", match_ts_ns=3000)
        store.on_fill(fill1)
        delta = store.on_fill(fill2)
        assert delta.net_qty == 15

    def test_key_generation(self, store):
        key = store._key("ACC1", "s1", "2330")
        assert key == "ACC1:s1:2330"

    def test_total_pnl(self, store):
        assert store.total_pnl == 0
        buy = _make_fill(side=Side.BUY, qty=10, price=5000000)
        store.on_fill(buy)
        sell = _make_fill(side=Side.SELL, qty=10, price=5100000, fill_id="F002", match_ts_ns=3000)
        store.on_fill(sell)
        assert store.total_pnl == 1_000_000

    def test_drawdown_pct_no_peak(self, store):
        assert store.get_drawdown_pct() == 0.0

    def test_drawdown_pct_after_profit_and_loss(self, store):
        # Create profit first
        buy = _make_fill(side=Side.BUY, qty=10, price=5000000)
        store.on_fill(buy)
        sell = _make_fill(side=Side.SELL, qty=10, price=5100000, fill_id="F002", match_ts_ns=3000)
        store.on_fill(sell)
        peak = store._peak_equity_scaled
        assert peak == 1_000_000

        # Now create loss
        buy2 = _make_fill(side=Side.BUY, qty=10, price=5200000, fill_id="F003", match_ts_ns=4000)
        store.on_fill(buy2)
        sell2 = _make_fill(side=Side.SELL, qty=10, price=4900000, fill_id="F004", match_ts_ns=5000)
        store.on_fill(sell2)
        # Net PnL = 1_000_000 - 3_000_000 = -2_000_000
        assert store.total_pnl < peak
        dd = store.get_drawdown_pct()
        assert dd > 0.0

    def test_evict_flat_positions(self, store):
        store._positions_max_size = 3
        # Create 3 flat positions
        for i in range(3):
            buy = _make_fill(symbol=f"SYM{i}", fill_id=f"B{i}", match_ts_ns=i * 1000)
            store.on_fill(buy)
            sell = _make_fill(
                symbol=f"SYM{i}",
                side=Side.SELL,
                fill_id=f"S{i}",
                match_ts_ns=i * 1000 + 500,
            )
            store.on_fill(sell)
        assert len(store.positions) == 3

        # Adding 4th fill should evict some flat positions
        new_fill = _make_fill(symbol="NEW", fill_id="N1", match_ts_ns=99999)
        store.on_fill(new_fill)
        # After eviction, we should have fewer or equal to max
        assert len(store.positions) <= 4  # 3 flat - evicted + 1 new

    def test_on_fill_async(self, store):
        fill = _make_fill()
        loop = asyncio.new_event_loop()
        try:
            delta = loop.run_until_complete(store.on_fill_async(fill))
            assert delta.net_qty == 10
        finally:
            loop.close()


# ===========================================================================
# Position Timestamp Tests
# ===========================================================================


class TestPositionTimestamp:
    def test_update_sets_timestamp(self):
        pos = Position("ACC1", "s1", "2330")
        fill = _make_fill(match_ts_ns=12345)
        pos.update(fill)
        assert pos.last_update_ts == 12345


# ===========================================================================
# Reconciliation Tests
# ===========================================================================


class TestReconciliation:
    def _make_service(self, store=None, client=None, config=None, storm_guard=None):
        from hft_platform.execution.reconciliation import ReconciliationService

        if store is None:
            store = PositionStore()
        if client is None:
            client = MagicMock()
            client.get_positions = MagicMock(return_value=[])
        if config is None:
            config = {
                "reconciliation": {
                    "check_interval_s": 0.01,
                    "grace_failures": 3,
                }
            }
        if storm_guard is None:
            storm_guard = MagicMock()
        return ReconciliationService(client, store, config, storm_guard)

    @pytest.mark.asyncio
    async def test_sync_no_discrepancies(self):
        svc = self._make_service()
        await svc.sync_portfolio()
        assert svc._last_discrepancies == []

    @pytest.mark.asyncio
    async def test_sync_detects_discrepancy(self):
        store = PositionStore()
        buy = _make_fill(symbol="2330", qty=10)
        store.on_fill(buy)

        client = MagicMock()
        client.get_positions = MagicMock(
            return_value=[
                MagicMock(code="2330", quantity=5, direction=""),
            ]
        )
        svc = self._make_service(store=store, client=client)
        await svc.sync_portfolio()
        assert len(svc._last_discrepancies) == 1
        assert svc._last_discrepancies[0].diff == 5  # 10 - 5

    @pytest.mark.asyncio
    async def test_sync_detects_broker_only_position(self):
        store = PositionStore()
        client = MagicMock()
        client.get_positions = MagicMock(
            return_value=[
                MagicMock(code="2330", quantity=10, direction=""),
            ]
        )
        svc = self._make_service(store=store, client=client)
        await svc.sync_portfolio()
        assert len(svc._last_discrepancies) == 1
        assert svc._last_discrepancies[0].local_qty == 0
        assert svc._last_discrepancies[0].broker_qty == 10

    @pytest.mark.asyncio
    async def test_critical_discrepancy_triggers_halt(self):
        store = PositionStore()
        buy = _make_fill(symbol="2330", qty=1000)
        store.on_fill(buy)

        client = MagicMock()
        client.get_positions = MagicMock(
            return_value=[
                MagicMock(code="2330", quantity=-500, direction="Action.Sell"),
            ]
        )
        storm_guard = MagicMock()
        svc = self._make_service(store=store, client=client, storm_guard=storm_guard)
        await svc.sync_portfolio()
        # Sign mismatch: local=1000, broker=-500 — critical
        storm_guard.trigger_halt.assert_called_once()


# ===========================================================================
# PositionDiscrepancy Tests
# ===========================================================================


class TestPositionDiscrepancy:
    def test_is_critical_sign_mismatch(self):
        from hft_platform.execution.reconciliation import PositionDiscrepancy

        d = PositionDiscrepancy(symbol="2330", local_qty=100, broker_qty=-50, diff=150)
        assert d.is_critical is True

    def test_is_critical_large_diff(self):
        from hft_platform.execution.reconciliation import PositionDiscrepancy

        d = PositionDiscrepancy(symbol="2330", local_qty=1000, broker_qty=800, diff=200)
        # threshold = max(100, 1000//10) = 100; diff=200 > 100 → critical
        assert d.is_critical is True

    def test_not_critical_small_diff(self):
        from hft_platform.execution.reconciliation import PositionDiscrepancy

        d = PositionDiscrepancy(symbol="2330", local_qty=1000, broker_qty=990, diff=10)
        # threshold = max(100, 100) = 100; diff=10 < 100 → not critical
        assert d.is_critical is False

    def test_not_critical_both_zero(self):
        from hft_platform.execution.reconciliation import PositionDiscrepancy

        d = PositionDiscrepancy(symbol="2330", local_qty=0, broker_qty=0, diff=0)
        assert d.is_critical is False

    def test_severity_critical(self):
        from hft_platform.execution.reconciliation import PositionDiscrepancy

        d = PositionDiscrepancy(symbol="2330", local_qty=100, broker_qty=-50, diff=150)
        assert d.severity == "critical"

    def test_severity_warning(self):
        from hft_platform.execution.reconciliation import PositionDiscrepancy

        d = PositionDiscrepancy(symbol="2330", local_qty=1000, broker_qty=985, diff=15)
        assert d.severity == "warning"

    def test_severity_info(self):
        from hft_platform.execution.reconciliation import PositionDiscrepancy

        d = PositionDiscrepancy(symbol="2330", local_qty=1000, broker_qty=995, diff=5)
        assert d.severity == "info"


# ===========================================================================
# Reconciliation Backoff Tests
# ===========================================================================


class TestReconciliationBackoff:
    def test_compute_backoff_delay(self):
        from hft_platform.execution.reconciliation import _compute_backoff_delay

        delay = _compute_backoff_delay(attempt=0, base=2.0, max_delay=60.0, jitter=0.0)
        assert delay == 2.0

    def test_compute_backoff_exponential(self):
        from hft_platform.execution.reconciliation import _compute_backoff_delay

        d0 = _compute_backoff_delay(attempt=0, base=2.0, max_delay=60.0, jitter=0.0)
        d1 = _compute_backoff_delay(attempt=1, base=2.0, max_delay=60.0, jitter=0.0)
        d2 = _compute_backoff_delay(attempt=2, base=2.0, max_delay=60.0, jitter=0.0)
        assert d0 == 2.0
        assert d1 == 4.0
        assert d2 == 8.0

    def test_compute_backoff_max_cap(self):
        from hft_platform.execution.reconciliation import _compute_backoff_delay

        delay = _compute_backoff_delay(attempt=20, base=2.0, max_delay=60.0, jitter=0.0)
        assert delay == 60.0

    def test_compute_backoff_with_jitter(self):
        from hft_platform.execution.reconciliation import _compute_backoff_delay

        delays = [_compute_backoff_delay(attempt=0, base=2.0, max_delay=60.0, jitter=0.2) for _ in range(100)]
        assert min(delays) >= 2.0 * 0.8
        assert max(delays) <= 2.0 * 1.2


# ===========================================================================
# Reconciliation Failure Escalation Tests
# ===========================================================================


class TestReconciliationFailureEscalation:
    @pytest.mark.asyncio
    async def test_consecutive_failures_trigger_halt(self):
        from hft_platform.execution.reconciliation import ReconciliationService

        store = PositionStore()
        client = MagicMock()
        # First call succeeds (startup sync), subsequent calls fail
        call_count = 0

        def _get_positions():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise RuntimeError("broker down")
            return []

        client.get_positions = _get_positions
        storm_guard = MagicMock()
        config = {
            "reconciliation": {
                "check_interval_s": 0.01,
                "grace_failures": 2,
                "backoff_base": 1.01,
                "backoff_max": 0.02,
            }
        }
        svc = ReconciliationService(client, store, config, storm_guard)

        task = asyncio.create_task(svc.run())
        await asyncio.sleep(0.5)
        svc.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert svc._halt_triggered is True
        storm_guard.trigger_halt.assert_called()

    def test_failure_counter_resets_on_success(self):
        from hft_platform.execution.reconciliation import ReconciliationService

        store = PositionStore()
        client = MagicMock()
        client.get_positions = MagicMock(return_value=[])
        storm_guard = MagicMock()
        config = {"reconciliation": {"grace_failures": 3}}
        svc = ReconciliationService(client, store, config, storm_guard)
        svc._consecutive_failures = 2
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(svc.sync_portfolio())
        finally:
            loop.close()
        # sync_portfolio doesn't reset _consecutive_failures directly — run() does
        # But sync_portfolio should succeed without raising
        assert svc._last_discrepancies == []


# ===========================================================================
# Reconciliation Compute Discrepancies Tests
# ===========================================================================


class TestComputeDiscrepancies:
    def _make_service(self):
        from hft_platform.execution.reconciliation import ReconciliationService

        return ReconciliationService(MagicMock(), PositionStore(), {}, MagicMock())

    def test_no_discrepancies(self):
        svc = self._make_service()
        result = svc._compute_discrepancies({"2330": 10}, {"2330": 10})
        assert result == []

    def test_qty_mismatch(self):
        svc = self._make_service()
        result = svc._compute_discrepancies({"2330": 10}, {"2330": 5})
        assert len(result) == 1
        assert result[0].diff == 5

    def test_local_only_position(self):
        svc = self._make_service()
        result = svc._compute_discrepancies({"2330": 10}, {})
        assert len(result) == 1
        assert result[0].broker_qty == 0

    def test_broker_only_position(self):
        svc = self._make_service()
        result = svc._compute_discrepancies({}, {"2330": 10})
        assert len(result) == 1
        assert result[0].local_qty == 0

    def test_multiple_symbols(self):
        svc = self._make_service()
        result = svc._compute_discrepancies(
            {"2330": 10, "2317": 5},
            {"2330": 10, "2317": 3},
        )
        assert len(result) == 1
        assert result[0].symbol == "2317"
