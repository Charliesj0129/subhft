"""Tests for contract_multiplier in PnL calculation (futures vs stocks).

Futures PnL = (exit - entry) * qty * point_value
Stocks  PnL = (exit - entry) * qty * 1

Without contract_multiplier, futures PnL is underestimated by point_value factor.
"""

from __future__ import annotations

import pytest

from hft_platform.contracts.execution import FillEvent, Side


def _make_fill(symbol: str, side: Side, qty: int, price: int, **kw) -> FillEvent:
    """Create a FillEvent with sensible defaults."""
    return FillEvent(
        fill_id=kw.get("fill_id", "test_fill"),
        account_id=kw.get("account_id", "acc"),
        order_id=kw.get("order_id", "test_order"),
        strategy_id=kw.get("strategy_id", "strat"),
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=kw.get("fee", 0),
        tax=kw.get("tax", 0),
        ingest_ts_ns=kw.get("ingest_ts_ns", 1_000_000_000),
        match_ts_ns=kw.get("match_ts_ns", 1_000_000_000),
    )


class TestPythonPositionMultiplier:
    """Test Position.update() with contract_multiplier."""

    def test_stock_pnl_multiplier_1(self):
        """Stock: multiplier=1, PnL = (exit-entry) * qty."""
        from hft_platform.execution.positions import Position

        pos = Position("acc", "strat", "2330")
        pos.update(_make_fill("2330", Side.BUY, 10, 5_000_000), contract_multiplier=1)
        assert pos.net_qty == 10
        assert pos.realized_pnl_scaled == 0

        pos.update(_make_fill("2330", Side.SELL, 10, 5_100_000), contract_multiplier=1)
        assert pos.net_qty == 0
        assert pos.realized_pnl_scaled == 1_000_000

    def test_futures_pnl_multiplier_10(self):
        """TMF (微台指): multiplier=10, PnL = (exit-entry) * qty * 10."""
        from hft_platform.execution.positions import Position

        pos = Position("acc", "strat", "TMFD6")
        pos.update(_make_fill("TMFD6", Side.BUY, 1, 333_450_000), contract_multiplier=10)
        assert pos.net_qty == 1

        pos.update(_make_fill("TMFD6", Side.SELL, 1, 333_440_000), contract_multiplier=10)
        assert pos.net_qty == 0
        assert pos.realized_pnl_scaled == -100_000

    def test_futures_pnl_multiplier_50(self):
        """MXF (小台指): multiplier=50, PnL = (exit-entry) * qty * 50."""
        from hft_platform.execution.positions import Position

        pos = Position("acc", "strat", "MXFD6")
        pos.update(_make_fill("MXFD6", Side.BUY, 1, 333_450_000), contract_multiplier=50)
        pos.update(_make_fill("MXFD6", Side.SELL, 1, 333_440_000), contract_multiplier=50)
        assert pos.realized_pnl_scaled == -500_000

    def test_futures_pnl_multiplier_200(self):
        """TXF (台指期): multiplier=200, PnL = (exit-entry) * qty * 200."""
        from hft_platform.execution.positions import Position

        pos = Position("acc", "strat", "TXFD6")
        pos.update(_make_fill("TXFD6", Side.BUY, 1, 333_450_000), contract_multiplier=200)
        pos.update(_make_fill("TXFD6", Side.SELL, 1, 333_440_000), contract_multiplier=200)
        assert pos.realized_pnl_scaled == -2_000_000

    def test_default_multiplier_is_1(self):
        """Without explicit multiplier, default is 1 (backward compat for stocks)."""
        from hft_platform.execution.positions import Position

        pos = Position("acc", "strat", "2330")
        pos.update(_make_fill("2330", Side.BUY, 10, 1_000))
        pos.update(_make_fill("2330", Side.SELL, 10, 1_050))
        assert pos.realized_pnl_scaled == 500

    def test_short_futures_pnl(self):
        """Short futures: PnL = (entry-exit) * qty * multiplier."""
        from hft_platform.execution.positions import Position

        pos = Position("acc", "strat", "TMFD6")
        pos.update(_make_fill("TMFD6", Side.SELL, 1, 333_450_000), contract_multiplier=10)
        pos.update(_make_fill("TMFD6", Side.BUY, 1, 333_400_000), contract_multiplier=10)
        assert pos.realized_pnl_scaled == 500_000


class TestRustPositionMultiplier:
    """Test RustPositionTracker.update() with contract_multiplier."""

    def _get_tracker(self):
        try:
            from hft_platform.rust_core import RustPositionTracker

            return RustPositionTracker()
        except ImportError:
            pytest.skip("Rust extension not available")

    def test_rust_futures_multiplier_10(self):
        tracker = self._get_tracker()
        key = "acc:strat:TMFD6"
        tracker.update(key, 0, 1, 333_450_000, 0, 0, 100, 10)
        net, avg, pnl, fees = tracker.update(key, 1, 1, 333_440_000, 0, 0, 200, 10)
        assert net == 0
        assert pnl == -100_000

    def test_rust_default_multiplier_1(self):
        tracker = self._get_tracker()
        key = "acc:strat:2330"
        tracker.update(key, 0, 10, 1000, 5, 0, 100, 1)
        net, avg, pnl, fees = tracker.update(key, 1, 10, 1050, 5, 0, 200, 1)
        assert net == 0
        assert pnl == 500

    def test_rust_multiplier_backward_compat(self):
        """Calling update() without multiplier arg should default to 1."""
        tracker = self._get_tracker()
        key = "acc:strat:SYM"
        tracker.update(key, 0, 10, 1000, 0, 0, 100)
        net, avg, pnl, fees = tracker.update(key, 1, 10, 1050, 0, 0, 200)
        assert pnl == 500


class TestPositionStoreMultiplier:
    """Test that PositionStore reads contract_multiplier from SymbolMetadata."""

    def test_store_uses_metadata_point_value(self):
        """PositionStore should read point_value from metadata for multiplier."""
        from hft_platform.execution.positions import PositionStore

        store = PositionStore()

        tmf_meta = store.metadata.meta.get("TMFD6", {})
        if not tmf_meta:
            pytest.skip("TMFD6 not in symbols.yaml")

        point_value = tmf_meta.get("point_value", 1)
        assert point_value == 10, f"TMFD6 point_value should be 10, got {point_value}"

    def test_store_pnl_uses_multiplier(self):
        """Full PositionStore.on_fill() path should use contract_multiplier."""
        from hft_platform.execution.positions import PositionStore

        store = PositionStore()
        store._rust_tracker = None
        store.metadata.meta["TMFD6"] = {"point_value": 10}

        buy_fill = _make_fill("TMFD6", Side.BUY, 1, 333_450_000)
        sell_fill = _make_fill("TMFD6", Side.SELL, 1, 333_440_000)

        store.on_fill(buy_fill)
        store.on_fill(sell_fill)

        key = "acc:strat:TMFD6"
        pos = store.positions.get(key)
        assert pos is not None, f"Position not found for {key}"
        assert pos.realized_pnl_scaled == -100_000, (
            f"PnL should be -100,000 (with multiplier=10), got {pos.realized_pnl_scaled}"
        )
