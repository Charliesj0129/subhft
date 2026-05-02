"""Unit 7: E2E Full Trading Cycle Integration Test.

Tests the critical path: fill -> position update -> PnL calculation.
All prices use scaled integers (x10000) per the Precision Law.
"""

import time

import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
from hft_platform.execution.positions import Position, PositionStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fill(
    *,
    side: Side,
    qty: int,
    price: int,
    fee: int = 0,
    tax: int = 0,
    fill_id: str = "F001",
    account_id: str = "ACC01",
    order_id: str = "ORD001",
    strategy_id: str = "strat_mm",
    symbol: str = "2330",
) -> FillEvent:
    ts = time.time_ns()
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
        ingest_ts_ns=ts,
        match_ts_ns=ts,
    )


# ---------------------------------------------------------------------------
# Test 1: Single fill updates position with scaled int
# ---------------------------------------------------------------------------


class TestFillUpdatesPosition:
    """Verify that a single BUY fill creates a position with correct scaled-int fields."""

    def test_fill_updates_position_scaled_int(self):
        store = PositionStore()

        fill = _make_fill(
            side=Side.BUY,
            qty=1,
            price=6_000_000,  # 600.0000 in x10000
            fee=1_425,  # 0.1425 in x10000
            tax=0,
        )

        delta = store.on_fill(fill)

        key = "ACC01:strat_mm:2330"
        pos = store.positions[key]

        assert pos.net_qty == 1
        assert pos.avg_price_scaled == 6_000_000
        assert pos.fees_scaled == 1_425
        assert pos.realized_pnl_scaled == 0

        # PositionDelta should mirror the position state
        assert delta.net_qty == 1
        assert delta.avg_price == 6_000_000
        assert delta.realized_pnl == 0


# ---------------------------------------------------------------------------
# Test 2: Buy then sell computes PnL
# ---------------------------------------------------------------------------


class TestBuyThenSellPnL:
    """Buy 1 @ 600.0000, sell 1 @ 610.0000 => realized PnL = 10.0000 * 10000 = 100_000."""

    def test_buy_then_sell_computes_pnl(self):
        store = PositionStore()

        buy = _make_fill(
            side=Side.BUY,
            qty=1,
            price=6_000_000,
            fee=1_425,
            tax=0,
            fill_id="F001",
            order_id="ORD001",
        )
        store.on_fill(buy)

        sell = _make_fill(
            side=Side.SELL,
            qty=1,
            price=6_100_000,
            fee=1_425,
            tax=9_000,
            fill_id="F002",
            order_id="ORD002",
        )
        delta = store.on_fill(sell)

        key = "ACC01:strat_mm:2330"
        pos = store.positions[key]

        assert pos.net_qty == 0
        # PnL = (sell_price - buy_price) * qty = (6_100_000 - 6_000_000) * 1
        assert pos.realized_pnl_scaled == 100_000
        # Total fees: 1425 + (1425 + 9000)
        assert pos.fees_scaled == 1_425 + 1_425 + 9_000

        assert delta.net_qty == 0
        assert delta.realized_pnl == 100_000

    def test_sell_then_buy_short_pnl(self):
        """Short sell 1 @ 610, cover buy 1 @ 600 => PnL = +100_000."""
        store = PositionStore()

        sell = _make_fill(side=Side.SELL, qty=1, price=6_100_000, fill_id="F001")
        store.on_fill(sell)

        buy = _make_fill(side=Side.BUY, qty=1, price=6_000_000, fill_id="F002")
        delta = store.on_fill(buy)

        key = "ACC01:strat_mm:2330"
        pos = store.positions[key]

        assert pos.net_qty == 0
        # Short PnL = (entry - exit) * qty = (6_100_000 - 6_000_000) * 1
        assert pos.realized_pnl_scaled == 100_000
        assert delta.realized_pnl == 100_000


# ---------------------------------------------------------------------------
# Test 3: Multiple fills weighted average price
# ---------------------------------------------------------------------------


class TestMultipleFillsWeightedAverage:
    """BUY 2 @ 6_000_000 then BUY 3 @ 6_200_000 => avg = 6_120_000."""

    def test_multiple_fills_weighted_average(self):
        store = PositionStore()

        fill1 = _make_fill(
            side=Side.BUY,
            qty=2,
            price=6_000_000,
            fill_id="F001",
            order_id="ORD001",
        )
        store.on_fill(fill1)

        fill2 = _make_fill(
            side=Side.BUY,
            qty=3,
            price=6_200_000,
            fill_id="F002",
            order_id="ORD002",
        )
        store.on_fill(fill2)

        key = "ACC01:strat_mm:2330"
        pos = store.positions[key]

        assert pos.net_qty == 5
        # Weighted avg = (2 * 6_000_000 + 3 * 6_200_000) / 5 = 6_120_000
        assert pos.avg_price_scaled == 6_120_000
        assert pos.realized_pnl_scaled == 0


# ---------------------------------------------------------------------------
# Test 4: Portfolio-level drawdown tracking
# ---------------------------------------------------------------------------


class TestPortfolioDrawdown:
    """Verify portfolio-level PnL aggregation and drawdown calculation."""

    def test_drawdown_after_loss(self, monkeypatch):
        # Bug B (2026-04-20) raised _MIN_PEAK_SCALED from 2_000_000 (200 NTD)
        # to 100_000_000 (10,000 NTD) in positions.py. Peak in this scenario
        # is 200_000 (20 NTD), well below the new cold-start guard, so
        # get_drawdown_pct() returns 0.0 unconditionally and never reaches
        # the math under test. Lower the guard to 0 to exercise the actual
        # peak-vs-current calculation. Same monkeypatch pattern as
        # tests/unit/test_position_store_unit.py:54.
        import hft_platform.execution.positions as _positions

        monkeypatch.setattr(_positions, "_MIN_PEAK_SCALED", 0)
        store = PositionStore()

        # Profitable round-trip on symbol A
        store.on_fill(_make_fill(side=Side.BUY, qty=1, price=6_000_000, symbol="A", fill_id="F1"))
        store.on_fill(_make_fill(side=Side.SELL, qty=1, price=6_200_000, symbol="A", fill_id="F2"))
        assert store.total_pnl == 200_000
        assert store.get_drawdown_pct() == 0.0

        # Losing round-trip on symbol B
        store.on_fill(_make_fill(side=Side.BUY, qty=1, price=6_000_000, symbol="B", fill_id="F3"))
        store.on_fill(_make_fill(side=Side.SELL, qty=1, price=5_500_000, symbol="B", fill_id="F4"))

        # Net PnL = 200_000 - 500_000 = -300_000, peak was 200_000
        assert store.total_pnl == -300_000
        # Drawdown = (200_000 - (-300_000)) / 200_000 = 500_000 / 200_000 = 2.5
        assert store.get_drawdown_pct() == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# Test 5: Partial close with remaining position
# ---------------------------------------------------------------------------


class TestPartialClose:
    """BUY 5, SELL 2 => net_qty=3, partial PnL realized."""

    def test_partial_close(self):
        store = PositionStore()

        store.on_fill(_make_fill(side=Side.BUY, qty=5, price=6_000_000, fill_id="F1"))
        delta = store.on_fill(_make_fill(side=Side.SELL, qty=2, price=6_100_000, fill_id="F2"))

        key = "ACC01:strat_mm:2330"
        pos = store.positions[key]

        assert pos.net_qty == 3
        # avg_price should remain 6_000_000 (only closing, not opening new)
        assert pos.avg_price_scaled == 6_000_000
        # PnL on closed portion: (6_100_000 - 6_000_000) * 2 = 200_000
        assert pos.realized_pnl_scaled == 200_000
        assert delta.net_qty == 3


# ---------------------------------------------------------------------------
# Test 6: Position.update directly (unit-level)
# ---------------------------------------------------------------------------


class TestPositionUpdateDirect:
    """Test Position.update() method directly for correctness."""

    def test_flat_to_long(self):
        pos = Position(account_id="A", strategy_id="S", symbol="X")
        fill = _make_fill(side=Side.BUY, qty=3, price=5_000_000)
        pos.update(fill)

        assert pos.net_qty == 3
        assert pos.avg_price_scaled == 5_000_000

    def test_long_to_flat(self):
        pos = Position(account_id="A", strategy_id="S", symbol="X")
        pos.update(_make_fill(side=Side.BUY, qty=2, price=5_000_000, fill_id="F1"))
        pos.update(_make_fill(side=Side.SELL, qty=2, price=5_500_000, fill_id="F2"))

        assert pos.net_qty == 0
        assert pos.realized_pnl_scaled == 1_000_000  # (5_500_000 - 5_000_000) * 2

    def test_flip_long_to_short(self):
        """BUY 2 @ 5M, SELL 5 @ 5.1M => close 2 (PnL = +200k), open short 3 @ 5.1M."""
        pos = Position(account_id="A", strategy_id="S", symbol="X")
        pos.update(_make_fill(side=Side.BUY, qty=2, price=5_000_000, fill_id="F1"))
        pos.update(_make_fill(side=Side.SELL, qty=5, price=5_100_000, fill_id="F2"))

        assert pos.net_qty == -3
        assert pos.avg_price_scaled == 5_100_000
        # PnL from closing 2 long: (5_100_000 - 5_000_000) * 2 = 200_000
        assert pos.realized_pnl_scaled == 200_000


# ---------------------------------------------------------------------------
# Test 7: All prices are int (Precision Law enforcement)
# ---------------------------------------------------------------------------


class TestPrecisionLaw:
    """Ensure no float contamination in position accounting."""

    def test_all_position_fields_are_int(self):
        store = PositionStore()
        store.on_fill(_make_fill(side=Side.BUY, qty=1, price=6_000_000, fee=1_425))
        store.on_fill(_make_fill(side=Side.SELL, qty=1, price=6_100_000, fee=1_425, tax=9_000, fill_id="F2"))

        key = "ACC01:strat_mm:2330"
        pos = store.positions[key]

        assert isinstance(pos.net_qty, int)
        assert isinstance(pos.avg_price_scaled, int)
        assert isinstance(pos.realized_pnl_scaled, int)
        assert isinstance(pos.fees_scaled, int)
        assert isinstance(pos.last_update_ts, int)
