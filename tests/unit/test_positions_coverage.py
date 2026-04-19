"""Coverage gap tests for execution/positions.py.

Targets uncovered branches: drawdown calculation, net_qty_for_symbol with
recovery, mark_to_market with recovery positions, clear_symbol_positions,
snapshot_positions, _evict_flat_positions, Position.update flip-side,
descale methods, load_recovery edge cases, and on_fill_async.
"""

from __future__ import annotations

import asyncio

import pytest

from hft_platform.contracts.execution import FillEvent, PositionDelta, Side
from hft_platform.execution.positions import Position, PositionStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fill(**kwargs):
    defaults = dict(
        fill_id="f1",
        account_id="acc1",
        order_id="o1",
        strategy_id="s1",
        symbol="2330",
        side=Side.BUY,
        qty=10,
        price=1000000,  # Scaled int x10000
        fee=100,
        tax=0,
        ingest_ts_ns=0,
        match_ts_ns=1_000_000_000,
    )
    defaults.update(kwargs)
    return FillEvent(**defaults)


# ---------------------------------------------------------------------------
# Position dataclass
# ---------------------------------------------------------------------------


class TestPosition:
    def test_properties(self):
        pos = Position("acc", "s1", "2330", net_qty=5, avg_price_scaled=1000000)
        assert pos.avg_price == 1000000
        assert pos.realized_pnl == 0
        assert pos.fees == 0

    def test_descale_methods(self):
        pos = Position("acc", "s1", "2330", avg_price_scaled=5000000, realized_pnl_scaled=100000, fees_scaled=5000)
        assert pos.descaled_avg_price(10000) == 500.0
        assert pos.descaled_realized_pnl(10000) == 10.0
        assert pos.descaled_fees(10000) == 0.5

    def test_descale_zero_scale(self):
        pos = Position("acc", "s1", "2330", avg_price_scaled=5000000)
        assert pos.descaled_avg_price(0) == 0.0
        assert pos.descaled_realized_pnl(0) == 0.0
        assert pos.descaled_fees(0) == 0.0

    def test_update_buy_open(self):
        pos = Position("acc", "s1", "2330")
        fill = _make_fill(side=Side.BUY, qty=10, price=1000000)
        pos.update(fill)
        assert pos.net_qty == 10
        assert pos.avg_price_scaled == 1000000

    def test_update_sell_close_long(self):
        pos = Position("acc", "s1", "2330", net_qty=10, avg_price_scaled=1000000)
        fill = _make_fill(side=Side.SELL, qty=5, price=1100000)
        pos.update(fill)
        assert pos.net_qty == 5
        # PnL = (1100000 - 1000000) * 5 * 1 = 500000
        assert pos.realized_pnl_scaled == 500000

    def test_update_buy_close_short(self):
        pos = Position("acc", "s1", "2330", net_qty=-10, avg_price_scaled=1000000)
        fill = _make_fill(side=Side.BUY, qty=5, price=900000)
        pos.update(fill)
        assert pos.net_qty == -5
        # PnL = (1000000 - 900000) * 5 * 1 = 500000
        assert pos.realized_pnl_scaled == 500000

    def test_update_flip_from_long_to_short(self):
        pos = Position("acc", "s1", "2330", net_qty=3, avg_price_scaled=1000000)
        fill = _make_fill(side=Side.SELL, qty=5, price=1100000)
        pos.update(fill)
        assert pos.net_qty == -2
        # Flip: avg_price becomes fill price for the new short
        assert pos.avg_price_scaled == 1100000

    def test_update_flip_from_short_to_long(self):
        pos = Position("acc", "s1", "2330", net_qty=-3, avg_price_scaled=1000000)
        fill = _make_fill(side=Side.BUY, qty=5, price=900000)
        pos.update(fill)
        assert pos.net_qty == 2
        assert pos.avg_price_scaled == 900000

    def test_update_close_to_flat(self):
        pos = Position("acc", "s1", "2330", net_qty=5, avg_price_scaled=1000000)
        fill = _make_fill(side=Side.SELL, qty=5, price=1000000)
        pos.update(fill)
        assert pos.net_qty == 0
        assert pos.avg_price_scaled == 0

    def test_update_increasing_position_weighted_avg(self):
        pos = Position("acc", "s1", "2330", net_qty=10, avg_price_scaled=1000000)
        fill = _make_fill(side=Side.BUY, qty=10, price=1200000)
        pos.update(fill)
        assert pos.net_qty == 20
        # Weighted avg: (10*1000000 + 10*1200000) / 20 = 1100000
        assert pos.avg_price_scaled == 1100000

    def test_update_accumulates_fees(self):
        pos = Position("acc", "s1", "2330")
        fill = _make_fill(fee=500, tax=200)
        pos.update(fill)
        assert pos.fees_scaled == 700

    def test_update_with_contract_multiplier(self):
        pos = Position("acc", "s1", "TXFD6", net_qty=1, avg_price_scaled=1000000)
        fill = _make_fill(symbol="TXFD6", side=Side.SELL, qty=1, price=1100000)
        pos.update(fill, contract_multiplier=200)
        # PnL = (1100000 - 1000000) * 1 * 200 = 20000000
        assert pos.realized_pnl_scaled == 20000000


# ---------------------------------------------------------------------------
# PositionStore basics
# ---------------------------------------------------------------------------


class TestPositionStore:
    def test_on_fill_creates_position(self):
        store = PositionStore()
        store._rust_tracker = None
        fill = _make_fill()
        delta = store.on_fill(fill)
        assert isinstance(delta, PositionDelta)
        assert delta.net_qty == 10

    def test_on_fill_async(self):
        store = PositionStore()
        store._rust_tracker = None
        fill = _make_fill()
        loop = asyncio.new_event_loop()
        delta = loop.run_until_complete(store.on_fill_async(fill))
        loop.close()
        assert delta.net_qty == 10

    def test_total_pnl(self):
        store = PositionStore()
        assert store.total_pnl == 0

    def test_get_drawdown_pct_no_peak(self):
        store = PositionStore()
        store._peak_equity_scaled = 0
        store._total_realized_pnl_scaled = 0
        assert store.get_drawdown_pct() == 0.0

    def test_get_drawdown_pct_negative_no_peak(self):
        """Bug 10 fix: cold-start guard returns 0.0 when peak < min threshold."""
        store = PositionStore()
        store._peak_equity_scaled = 0
        store._total_realized_pnl_scaled = -100
        dd = store.get_drawdown_pct()
        assert dd == 0.0  # cold-start guard: peak < 2M → 0.0

    def test_get_drawdown_pct_from_peak(self):
        store = PositionStore()
        store._peak_equity_scaled = 10_000_000  # above min threshold (2M)
        store._total_realized_pnl_scaled = 8_000_000
        dd = store.get_drawdown_pct()
        assert dd == pytest.approx(0.2)

    def test_get_drawdown_pct_at_peak(self):
        store = PositionStore()
        store._peak_equity_scaled = 10_000_000
        store._total_realized_pnl_scaled = 10_000_000
        assert store.get_drawdown_pct() == 0.0


# ---------------------------------------------------------------------------
# PositionStore: net_qty_for_symbol
# ---------------------------------------------------------------------------


class TestNetQtyForSymbol:
    def test_basic(self):
        store = PositionStore()
        store._rust_tracker = None
        fill = _make_fill()
        store.on_fill(fill)
        qty = store.net_qty_for_symbol("2330")
        assert qty == 10

    def test_with_strategy_filter(self):
        store = PositionStore()
        store._rust_tracker = None
        store.on_fill(_make_fill(strategy_id="s1"))
        store.on_fill(_make_fill(strategy_id="s2", fill_id="f2"))
        qty = store.net_qty_for_symbol("2330", strategy_id="s1")
        assert qty == 10

    def test_includes_recovery(self):
        store = PositionStore()
        store._recovery_positions = {
            "acc1:2330": {"symbol": "2330", "net_qty": 5, "strategy_id": ""},
        }
        qty = store.net_qty_for_symbol("2330")
        assert qty == 5

    def test_recovery_excluded_with_strategy_filter(self):
        store = PositionStore()
        store._recovery_positions = {
            "acc1:2330": {"symbol": "2330", "net_qty": 5, "strategy_id": ""},
        }
        qty = store.net_qty_for_symbol("2330", strategy_id="s1")
        # Legacy recovery without strategy_id excluded from filtered queries
        assert qty == 0

    def test_recovery_with_matching_strategy(self):
        store = PositionStore()
        store._recovery_positions = {
            "acc1:s1:2330": {"symbol": "2330", "net_qty": 5, "strategy_id": "s1"},
        }
        qty = store.net_qty_for_symbol("2330", strategy_id="s1")
        assert qty == 5


# ---------------------------------------------------------------------------
# PositionStore: load_recovery
# ---------------------------------------------------------------------------


class TestLoadRecovery:
    def test_load_recovery_basic(self):
        store = PositionStore()
        store.load_recovery("acc1", "2330", 10, 1000000)
        assert len(store._recovery_positions) == 1

    def test_load_recovery_zero_qty_ignored(self):
        store = PositionStore()
        store.load_recovery("acc1", "2330", 0, 1000000)
        assert len(store._recovery_positions) == 0

    def test_load_recovery_with_strategy(self):
        store = PositionStore()
        store.load_recovery("acc1", "2330", 10, 1000000, strategy_id="s1")
        assert "acc1:s1:2330" in store._recovery_positions

    def test_load_recovery_without_strategy(self):
        store = PositionStore()
        store.load_recovery("acc1", "2330", 10, 1000000)
        assert "acc1:2330" in store._recovery_positions


# ---------------------------------------------------------------------------
# PositionStore: mark_to_market
# ---------------------------------------------------------------------------


class TestMarkToMarket:
    def test_mtm_basic(self):
        store = PositionStore()
        store._rust_tracker = None
        fill = _make_fill(price=1000000, qty=10)
        store.on_fill(fill)
        mtm = store.mark_to_market({"2330": 1100000})
        # (1100000 - 1000000) * 10 * 1 = 1000000
        assert mtm == 1000000

    def test_mtm_no_mid_price(self):
        store = PositionStore()
        store._rust_tracker = None
        store.on_fill(_make_fill())
        mtm = store.mark_to_market({})  # No mid price for 2330
        assert mtm == 0

    def test_mtm_flat_position_ignored(self):
        store = PositionStore()
        store._rust_tracker = None
        store.on_fill(_make_fill(side=Side.BUY, qty=10))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, fill_id="f2"))
        mtm = store.mark_to_market({"2330": 1100000})
        assert mtm == 0

    def test_mtm_includes_recovery(self):
        store = PositionStore()
        store._recovery_positions = {
            "acc1:2330": {
                "symbol": "2330",
                "net_qty": 5,
                "avg_price_scaled": 1000000,
            },
        }
        mtm = store.mark_to_market({"2330": 1100000})
        assert mtm == 500000

    def test_mtm_recovery_sentinel_skipped(self):
        """Recovery with sentinel avg_price (-1) is skipped in MtM."""
        store = PositionStore()
        store._recovery_positions = {
            "acc1:2330": {
                "symbol": "2330",
                "net_qty": 5,
                "avg_price_scaled": -1,
            },
        }
        mtm = store.mark_to_market({"2330": 1100000})
        assert mtm == 0


# ---------------------------------------------------------------------------
# PositionStore: snapshot_positions
# ---------------------------------------------------------------------------


class TestSnapshotPositions:
    def test_snapshot_is_deep_copy(self):
        store = PositionStore()
        store._rust_tracker = None
        store.on_fill(_make_fill())
        snap = store.snapshot_positions()
        # Modify the snapshot
        for pos in snap.values():
            pos.net_qty = 999
        # Original should be unchanged
        for pos in store.positions.values():
            assert pos.net_qty == 10


# ---------------------------------------------------------------------------
# PositionStore: reset and clear_symbol_positions
# ---------------------------------------------------------------------------


class TestPositionStoreReset:
    def test_reset(self):
        store = PositionStore()
        store._rust_tracker = None
        store.on_fill(_make_fill())
        count = store.reset()
        assert count == 1
        assert len(store.positions) == 0
        assert store._total_realized_pnl_scaled == 0

    def test_clear_symbol_positions(self):
        store = PositionStore()
        store._rust_tracker = None
        store.on_fill(_make_fill(symbol="2330"))
        store.on_fill(_make_fill(symbol="2317", fill_id="f2"))
        removed = store.clear_symbol_positions("2330")
        assert removed == 1
        assert len(store.positions) == 1

    def test_clear_symbol_with_recovery(self):
        store = PositionStore()
        store._recovery_positions = {
            "acc1:2330": {"symbol": "2330", "net_qty": 5, "avg_price_scaled": 1000000},
        }
        removed = store.clear_symbol_positions("2330")
        assert len(store._recovery_positions) == 0


# ---------------------------------------------------------------------------
# PositionStore: _evict_flat_positions
# ---------------------------------------------------------------------------


class TestEvictFlatPositions:
    def test_evict_flat(self):
        store = PositionStore()
        store._positions_max_size = 2
        store._rust_tracker = None
        # Create a flat position
        store.positions["acc:s1:A"] = Position("acc", "s1", "A", net_qty=0, last_update_ts=1)
        store.positions["acc:s1:B"] = Position("acc", "s1", "B", net_qty=5, last_update_ts=2)
        # Adding a third should trigger eviction
        fill = _make_fill(symbol="C", fill_id="f3")
        store.on_fill(fill)
        # Flat position A should be evicted
        assert "acc:s1:A" not in store.positions


# ---------------------------------------------------------------------------
# PositionStore: _update_portfolio_aggregates
# ---------------------------------------------------------------------------


class TestPortfolioAggregates:
    def test_update_with_delta(self):
        store = PositionStore()
        store._total_realized_pnl_scaled = 100
        store._peak_equity_scaled = 100
        store._update_portfolio_aggregates(50)
        assert store._total_realized_pnl_scaled == 150
        assert store._peak_equity_scaled == 150

    def test_update_without_delta_recomputes(self):
        store = PositionStore()
        store.positions["k"] = Position("a", "s", "x", realized_pnl_scaled=300)
        store._evicted_realized_pnl_scaled = 200
        store._update_portfolio_aggregates(0)
        assert store._total_realized_pnl_scaled == 500
