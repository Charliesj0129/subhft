"""Tests for PositionStore.mark_to_market() with contract_multiplier support.

All prices / PnL values use scaled integers (x10000).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.execution.positions import Position, PositionStore

SCALE = 10_000  # price scale factor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(*key_pos_pairs: tuple[str, Position]) -> PositionStore:
    """Build a PositionStore with positions pre-loaded, bypassing fill logic.

    Patches out metadata so tests control multiplier values directly.
    """
    store = PositionStore.__new__(PositionStore)
    store.positions = {key: pos for key, pos in key_pos_pairs}
    store.metadata = MagicMock()
    store.metadata.contract_multiplier.return_value = 1  # default stock multiplier
    return store


def _set_multiplier(store: PositionStore, symbol: str, multiplier: int) -> None:
    """Configure the mock metadata to return *multiplier* for *symbol*."""
    original = store.metadata.contract_multiplier.side_effect

    def _fn(sym: str) -> int:
        if sym == symbol:
            return multiplier
        if original is not None:
            return original(sym)
        return 1

    store.metadata.contract_multiplier.side_effect = _fn


# ---------------------------------------------------------------------------
# Tests: stock positions (multiplier = 1)
# ---------------------------------------------------------------------------


class TestMarkToMarketStock:
    def test_long_stock_positive_pnl(self):
        """Long 10 shares @ 100, mid=110 => unrealized = (110-100)*10*1 * SCALE."""
        pos = Position("acc", "strat", "STOCK")
        pos.net_qty = 10
        pos.avg_price_scaled = 100 * SCALE

        store = _make_store(("acc:strat:STOCK", pos))

        result = store.mark_to_market({"STOCK": 110 * SCALE})
        # (110-100) * 10 * 1 * SCALE = 1_000_000
        assert result == 10 * SCALE * 10

    def test_short_stock_positive_pnl(self):
        """Short 5 shares @ 200, mid=190 => unrealized = (190-200)*(-5) * SCALE."""
        pos = Position("acc", "strat", "STOCK")
        pos.net_qty = -5
        pos.avg_price_scaled = 200 * SCALE

        store = _make_store(("acc:strat:STOCK", pos))

        result = store.mark_to_market({"STOCK": 190 * SCALE})
        # (190-200) * (-5) * 1 = (-10)*(-5) = 50 * SCALE = 500_000
        assert result == 50 * SCALE

    def test_flat_position_contributes_zero(self):
        """Position with net_qty=0 is excluded from total."""
        pos = Position("acc", "strat", "FLAT")
        pos.net_qty = 0
        pos.avg_price_scaled = 100 * SCALE

        store = _make_store(("acc:strat:FLAT", pos))

        result = store.mark_to_market({"FLAT": 110 * SCALE})
        assert result == 0

    def test_missing_mid_price_excluded(self):
        """Symbol without a mid_price entry contributes 0."""
        pos = Position("acc", "strat", "NOSYM")
        pos.net_qty = 10
        pos.avg_price_scaled = 100 * SCALE

        store = _make_store(("acc:strat:NOSYM", pos))

        result = store.mark_to_market({})  # no prices at all
        assert result == 0


# ---------------------------------------------------------------------------
# Tests: futures positions (multiplier > 1)
# ---------------------------------------------------------------------------


class TestMarkToMarketFutures:
    def test_long_futures_tmf_multiplier_10(self):
        """Long 3 TMF @ 19000, mid=19050, multiplier=10 => pnl = (19050-19000)*3*10 * SCALE."""
        pos = Position("acc", "strat", "TMF")
        pos.net_qty = 3
        pos.avg_price_scaled = 19_000 * SCALE

        store = _make_store(("acc:strat:TMF", pos))
        _set_multiplier(store, "TMF", 10)

        result = store.mark_to_market({"TMF": 19_050 * SCALE})
        # (19050 - 19000) * 3 * 10 * SCALE = 50 * 3 * 10 * SCALE = 1_500_000
        assert result == 50 * SCALE * 3 * 10

    def test_long_futures_mxf_multiplier_50(self):
        """Long 2 MXF @ 19000, mid=19050, multiplier=50 => pnl = (19050-19000)*2*50 * SCALE."""
        pos = Position("acc", "strat", "MXF")
        pos.net_qty = 2
        pos.avg_price_scaled = 19_000 * SCALE

        store = _make_store(("acc:strat:MXF", pos))
        _set_multiplier(store, "MXF", 50)

        result = store.mark_to_market({"MXF": 19_050 * SCALE})
        # (19050 - 19000) * 2 * 50 * SCALE = 50 * 2 * 50 * SCALE = 5_000_000
        assert result == 50 * SCALE * 2 * 50

    def test_long_futures_txf_multiplier_200(self):
        """Long 1 TXF @ 20000, mid=20100, multiplier=200 => pnl = (20100-20000)*1*200 * SCALE."""
        pos = Position("acc", "strat", "TXF")
        pos.net_qty = 1
        pos.avg_price_scaled = 20_000 * SCALE

        store = _make_store(("acc:strat:TXF", pos))
        _set_multiplier(store, "TXF", 200)

        result = store.mark_to_market({"TXF": 20_100 * SCALE})
        # (20100 - 20000) * 1 * 200 * SCALE = 100 * 200 * SCALE = 20_000_000
        assert result == 100 * SCALE * 200

    def test_short_futures_positive_pnl_with_multiplier(self):
        """Short 1 TXF @ 20000, mid=19800, multiplier=200 => pnl = (19800-20000)*(-1)*200 * SCALE."""
        pos = Position("acc", "strat", "TXF")
        pos.net_qty = -1
        pos.avg_price_scaled = 20_000 * SCALE

        store = _make_store(("acc:strat:TXF", pos))
        _set_multiplier(store, "TXF", 200)

        result = store.mark_to_market({"TXF": 19_800 * SCALE})
        # (19800 - 20000) * (-1) * 200 * SCALE = (-200)*(-1)*200 = 40_000 * SCALE = 400_000_000
        assert result == 200 * SCALE * 200

    def test_futures_pnl_is_multiplier_times_stock_pnl(self):
        """Futures PnL is exactly multiplier times larger than equivalent stock PnL."""
        # Stock: long 5 @ 100, mid=110 => pnl = 10 * 5 * SCALE
        pos_stock = Position("acc", "strat", "STOCK")
        pos_stock.net_qty = 5
        pos_stock.avg_price_scaled = 100 * SCALE

        store_stock = _make_store(("acc:strat:STOCK", pos_stock))
        pnl_stock = store_stock.mark_to_market({"STOCK": 110 * SCALE})

        # Futures (multiplier=50): long 5 @ 100, mid=110 => pnl = 10 * 5 * 50 * SCALE
        pos_fut = Position("acc", "strat", "MXF")
        pos_fut.net_qty = 5
        pos_fut.avg_price_scaled = 100 * SCALE

        store_fut = _make_store(("acc:strat:MXF", pos_fut))
        _set_multiplier(store_fut, "MXF", 50)
        pnl_fut = store_fut.mark_to_market({"MXF": 110 * SCALE})

        assert pnl_fut == pnl_stock * 50


# ---------------------------------------------------------------------------
# Tests: mixed portfolio
# ---------------------------------------------------------------------------


class TestMarkToMarketMixed:
    def test_portfolio_sum_stock_and_futures(self):
        """Mixed stock + futures portfolio: total is sum of each position's PnL."""
        pos_stock = Position("acc", "strat", "STOCK")
        pos_stock.net_qty = 100
        pos_stock.avg_price_scaled = 50 * SCALE

        pos_fut = Position("acc", "strat", "TMF")
        pos_fut.net_qty = 2
        pos_fut.avg_price_scaled = 19_000 * SCALE

        store = _make_store(
            ("acc:strat:STOCK", pos_stock),
            ("acc:strat:TMF", pos_fut),
        )
        _set_multiplier(store, "TMF", 10)

        mid_prices = {
            "STOCK": 55 * SCALE,  # +5 per share
            "TMF": 19_100 * SCALE,  # +100 per contract
        }

        result = store.mark_to_market(mid_prices)

        # Stock: (55-50) * 100 * 1 * SCALE = 5_000_000
        stock_pnl = 5 * SCALE * 100
        # TMF: (19100-19000) * 2 * 10 * SCALE = 2_000_000
        fut_pnl = 100 * SCALE * 2 * 10

        assert result == stock_pnl + fut_pnl
