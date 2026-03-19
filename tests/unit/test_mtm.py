"""Tests for MarkToMarketCalculator (WU-03).

All prices / PnL values use scaled integers (x10000).
"""

from __future__ import annotations

from hft_platform.execution.mtm import MarkToMarketCalculator, portfolio_unrealized_pnl
from hft_platform.execution.positions import Position, PositionStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store_with_positions(positions: dict[str, Position]) -> PositionStore:
    """Build a PositionStore pre-loaded with given positions (no fills needed)."""
    store = PositionStore.__new__(PositionStore)
    store.positions = dict(positions)
    return store


def _mid_prices(mapping: dict[str, int]):
    """Return a mid-price callback backed by *mapping*."""

    def _fn(symbol: str) -> int | None:
        return mapping.get(symbol)

    return _fn


SCALE = 10_000  # price scale factor


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLongPnL:
    def test_long_positive_pnl(self):
        """Long 10 @ 100, mid=105 => unrealized = (105-100)*10 * SCALE."""
        pos = Position("acc", "strat", "SYM1")
        pos.net_qty = 10
        pos.avg_price_scaled = 100 * SCALE

        store = _make_store_with_positions({"acc:strat:SYM1": pos})
        calc = MarkToMarketCalculator(store, _mid_prices({"SYM1": 105 * SCALE}))

        result = calc.calculate()
        assert result["acc:strat:SYM1"] == 5 * SCALE * 10  # 500_000

    def test_long_negative_pnl(self):
        """Long 5 @ 200, mid=190 => unrealized = (190-200)*5 * SCALE."""
        pos = Position("acc", "strat", "SYM1")
        pos.net_qty = 5
        pos.avg_price_scaled = 200 * SCALE

        store = _make_store_with_positions({"acc:strat:SYM1": pos})
        calc = MarkToMarketCalculator(store, _mid_prices({"SYM1": 190 * SCALE}))

        result = calc.calculate()
        assert result["acc:strat:SYM1"] == -10 * SCALE * 5  # -500_000


class TestShortPnL:
    def test_short_positive_pnl(self):
        """Short 8 @ 150, mid=140 => unrealized = (150-140)*8 * SCALE."""
        pos = Position("acc", "strat", "SYM2")
        pos.net_qty = -8
        pos.avg_price_scaled = 150 * SCALE

        store = _make_store_with_positions({"acc:strat:SYM2": pos})
        calc = MarkToMarketCalculator(store, _mid_prices({"SYM2": 140 * SCALE}))

        result = calc.calculate()
        assert result["acc:strat:SYM2"] == 10 * SCALE * 8  # 800_000

    def test_short_negative_pnl(self):
        """Short 3 @ 100, mid=110 => unrealized = (100-110)*3 * SCALE."""
        pos = Position("acc", "strat", "SYM2")
        pos.net_qty = -3
        pos.avg_price_scaled = 100 * SCALE

        store = _make_store_with_positions({"acc:strat:SYM2": pos})
        calc = MarkToMarketCalculator(store, _mid_prices({"SYM2": 110 * SCALE}))

        result = calc.calculate()
        assert result["acc:strat:SYM2"] == -10 * SCALE * 3  # -300_000


class TestMultiplePositions:
    def test_portfolio_sum(self):
        """Two positions: total unrealized is their sum."""
        pos_a = Position("acc", "strat", "A")
        pos_a.net_qty = 10
        pos_a.avg_price_scaled = 100 * SCALE

        pos_b = Position("acc", "strat", "B")
        pos_b.net_qty = -5
        pos_b.avg_price_scaled = 200 * SCALE

        store = _make_store_with_positions(
            {
                "acc:strat:A": pos_a,
                "acc:strat:B": pos_b,
            }
        )
        mid_prices = {"A": 110 * SCALE, "B": 190 * SCALE}
        calc = MarkToMarketCalculator(store, _mid_prices(mid_prices))

        # A: (110-100)*10 = 100 * SCALE = 1_000_000
        # B: (200-190)*5  =  50 * SCALE =   500_000
        total = calc.total_unrealized_pnl()
        assert total == (100 * SCALE + 50 * SCALE)


class TestZeroPosition:
    def test_flat_position_returns_zero(self):
        pos = Position("acc", "strat", "FLAT")
        pos.net_qty = 0
        pos.avg_price_scaled = 100 * SCALE

        store = _make_store_with_positions({"acc:strat:FLAT": pos})
        calc = MarkToMarketCalculator(store, _mid_prices({"FLAT": 105 * SCALE}))

        result = calc.calculate()
        assert result["acc:strat:FLAT"] == 0


class TestMissingMidPrice:
    def test_missing_mid_price_skipped(self):
        """Position whose mid-price is unavailable is omitted from result."""
        pos = Position("acc", "strat", "NOSYM")
        pos.net_qty = 10
        pos.avg_price_scaled = 100 * SCALE

        store = _make_store_with_positions({"acc:strat:NOSYM": pos})
        calc = MarkToMarketCalculator(store, _mid_prices({}))  # no prices

        result = calc.calculate()
        assert "acc:strat:NOSYM" not in result

    def test_total_excludes_missing(self):
        """total_unrealized_pnl ignores positions without mid-price."""
        pos = Position("acc", "strat", "NOSYM")
        pos.net_qty = 10
        pos.avg_price_scaled = 100 * SCALE

        store = _make_store_with_positions({"acc:strat:NOSYM": pos})
        calc = MarkToMarketCalculator(store, _mid_prices({}))

        assert calc.total_unrealized_pnl() == 0


class TestMetricEmission:
    def test_gauge_updated_on_total(self):
        """Prometheus gauge is set when total_unrealized_pnl() is called."""
        pos = Position("acc", "strat", "X")
        pos.net_qty = 2
        pos.avg_price_scaled = 50 * SCALE

        store = _make_store_with_positions({"acc:strat:X": pos})
        calc = MarkToMarketCalculator(store, _mid_prices({"X": 60 * SCALE}))

        total = calc.total_unrealized_pnl()
        expected = (60 - 50) * SCALE * 2  # 200_000

        assert total == expected
        # Verify the gauge was set (read its internal value)
        assert portfolio_unrealized_pnl._value.get() == expected
