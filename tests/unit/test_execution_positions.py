"""Comprehensive pure-Python tests for Position and PositionStore.

Covers:
- Open long/short -> close -> PnL verification (scaled int arithmetic)
- Position flips (long->short, short->long) via oversized fills
- Partial close with weighted average price
- Fee/tax accumulation separate from PnL
- Eviction of flat positions on overflow
- Portfolio drawdown tracking
- Multi-symbol isolation
- PositionDelta return value correctness
- Zero-quantity edge case (flat position)

All tests force HFT_RUST_POSITIONS=0 via the position_store fixture.
"""

from __future__ import annotations

import itertools

import pytest

from hft_platform.contracts.execution import FillEvent, PositionDelta, Side
from hft_platform.execution.positions import Position

# ---------------------------------------------------------------------------
# Helpers — uses itertools.count() instead of mutable global for isolation
# ---------------------------------------------------------------------------

_fill_counter = itertools.count(1)


def _make_fill(
    side: Side,
    qty: int,
    price: int,
    *,
    fee: int = 0,
    tax: int = 0,
    account_id: str = "acc1",
    strategy_id: str = "strat1",
    symbol: str = "2330",
    match_ts_ns: int | None = None,
) -> FillEvent:
    seq = next(_fill_counter)
    if match_ts_ns is None:
        match_ts_ns = seq * 1_000_000
    return FillEvent(
        fill_id=f"F{seq:06d}",
        account_id=account_id,
        order_id=f"O{seq:06d}",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=match_ts_ns - 100,
        match_ts_ns=match_ts_ns,
    )


# NOTE: Uses the ``position_store`` fixture from tests/unit/conftest.py
# (aliased to ``store`` here for brevity).


@pytest.fixture()
def store(position_store):
    """Alias the shared position_store fixture for brevity."""
    return position_store


# ===========================================================================
# 1. Open long -> close -> PnL verification
# ===========================================================================


class TestOpenLongClosePnl:
    """Buy 10 @ 5_000_000, Sell 10 @ 5_100_000 -> PnL = 1_000_000."""

    def test_long_round_trip_pnl(self, store):
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000))
        delta = store.on_fill(_make_fill(Side.SELL, qty=10, price=5_100_000))

        assert delta.net_qty == 0
        assert delta.realized_pnl == 1_000_000
        assert isinstance(delta.realized_pnl, int), "PnL must be scaled int"


# ===========================================================================
# 2. Open short -> close -> PnL verification
# ===========================================================================


class TestOpenShortClosePnl:
    """Sell 10 @ 5_000_000, Buy 10 @ 4_900_000 -> PnL = 1_000_000."""

    def test_short_round_trip_pnl(self, store):
        store.on_fill(_make_fill(Side.SELL, qty=10, price=5_000_000))
        delta = store.on_fill(_make_fill(Side.BUY, qty=10, price=4_900_000))

        assert delta.net_qty == 0
        assert delta.realized_pnl == 1_000_000
        assert isinstance(delta.realized_pnl, int), "PnL must be scaled int"


# ===========================================================================
# 3. Position flip: long -> short via oversized fill
# ===========================================================================


class TestPositionFlipLongToShort:
    """Buy 5 @ 5_000_000, then Sell 10 @ 5_100_000.

    After: net_qty=-5, avg_price_scaled=5_100_000,
    realized_pnl = (5_100_000 - 5_000_000) * 5 = 500_000.
    Tests lines 128-129 of positions.py (flip branch).
    """

    def test_flip_long_to_short(self, store):
        store.on_fill(_make_fill(Side.BUY, qty=5, price=5_000_000))
        delta = store.on_fill(_make_fill(Side.SELL, qty=10, price=5_100_000))

        assert delta.net_qty == -5
        assert delta.avg_price == 5_100_000, "Avg price should reset to fill price on flip"
        assert delta.realized_pnl == 500_000

    def test_flip_position_internal_state(self, store):
        """Verify internal Position object state after flip."""
        store.on_fill(_make_fill(Side.BUY, qty=5, price=5_000_000))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=5_100_000))

        key = list(store.positions.keys())[0]
        pos = store.positions[key]
        assert pos.net_qty == -5
        assert pos.avg_price_scaled == 5_100_000
        assert pos.realized_pnl_scaled == 500_000


# ===========================================================================
# 4. Position flip: short -> long (mirror)
# ===========================================================================


class TestPositionFlipShortToLong:
    """Sell 5 @ 5_100_000, then Buy 10 @ 5_000_000.

    After: net_qty=+5, avg_price_scaled=5_000_000,
    realized_pnl = (5_100_000 - 5_000_000) * 5 = 500_000.
    """

    def test_flip_short_to_long(self, store):
        store.on_fill(_make_fill(Side.SELL, qty=5, price=5_100_000))
        delta = store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000))

        assert delta.net_qty == 5
        assert delta.avg_price == 5_000_000, "Avg price should reset to fill price on flip"
        assert delta.realized_pnl == 500_000


# ===========================================================================
# 5. Partial close with weighted average price
# ===========================================================================


class TestPartialCloseWeightedAverage:
    """Buy 10 @ 5_000_000, Buy 10 @ 5_200_000 -> avg = 5_100_000.

    Sell 5 -> avg unchanged, PnL = (sell_price - 5_100_000) * 5.
    """

    def test_weighted_avg_after_two_buys(self, store):
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000))
        d2 = store.on_fill(_make_fill(Side.BUY, qty=10, price=5_200_000))

        assert d2.net_qty == 20
        assert d2.avg_price == 5_100_000, "Weighted avg of 5M*10 + 5.2M*10 / 20"

    def test_partial_close_preserves_avg(self, store):
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000))
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_200_000))

        # Partial close: sell 5 @ 5_300_000
        d3 = store.on_fill(_make_fill(Side.SELL, qty=5, price=5_300_000))

        assert d3.net_qty == 15
        assert d3.avg_price == 5_100_000, "Avg price unchanged on partial close"
        # PnL = (5_300_000 - 5_100_000) * 5 = 1_000_000
        assert d3.realized_pnl == 1_000_000

    def test_partial_close_loss(self, store):
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000))
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_200_000))

        # Partial close at a loss: sell 5 @ 5_000_000
        d3 = store.on_fill(_make_fill(Side.SELL, qty=5, price=5_000_000))

        assert d3.net_qty == 15
        # PnL = (5_000_000 - 5_100_000) * 5 = -500_000
        assert d3.realized_pnl == -500_000


# ===========================================================================
# 6. Fee/tax accumulation
# ===========================================================================


class TestFeesTaxAccumulation:
    """Verify fees_scaled accumulates fee+tax from each fill, separate from PnL."""

    def test_fees_accumulate_across_fills(self, store):
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000, fee=1000, tax=500))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=5_100_000, fee=2000, tax=1000))

        key = list(store.positions.keys())[0]
        pos = store.positions[key]

        # fees_scaled = (1000+500) + (2000+1000) = 4500
        assert pos.fees_scaled == 4500
        # PnL is independent of fees
        assert pos.realized_pnl_scaled == 1_000_000

    def test_fees_do_not_affect_pnl(self, store):
        """Even with large fees, realized PnL stays pure."""
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000, fee=50_000, tax=25_000))
        delta = store.on_fill(_make_fill(Side.SELL, qty=10, price=5_000_000, fee=50_000, tax=25_000))

        # Break-even trade: PnL = 0
        assert delta.realized_pnl == 0

        key = list(store.positions.keys())[0]
        pos = store.positions[key]
        # Total fees = 2 * (50_000 + 25_000) = 150_000
        assert pos.fees_scaled == 150_000


# ===========================================================================
# 7. Eviction of flat positions on overflow
# ===========================================================================


class TestEvictFlatPositions:
    """Set _positions_max_size=2, fill 3 positions, close first to flat, open 4th -> eviction."""

    def test_eviction_triggers_on_overflow(self, store):
        store._positions_max_size = 2

        # Fill position 1 (close it to flat)
        store.on_fill(_make_fill(Side.BUY, qty=1, price=5_000_000, symbol="SYM_A", match_ts_ns=1_000_000))
        store.on_fill(_make_fill(Side.SELL, qty=1, price=5_000_000, symbol="SYM_A", match_ts_ns=2_000_000))

        # Fill position 2 (keep open)
        store.on_fill(_make_fill(Side.BUY, qty=1, price=5_000_000, symbol="SYM_B", match_ts_ns=3_000_000))

        # At this point we have 2 positions (SYM_A flat, SYM_B open)
        assert len(store.positions) == 2

        # Fill position 3 -> triggers eviction since size >= max
        store.on_fill(_make_fill(Side.BUY, qty=1, price=5_000_000, symbol="SYM_C", match_ts_ns=4_000_000))

        # SYM_A (flat) should have been evicted
        remaining_symbols = {pos.symbol for pos in store.positions.values()}
        assert "SYM_A" not in remaining_symbols, "Flat position SYM_A should be evicted"
        assert "SYM_B" in remaining_symbols
        assert "SYM_C" in remaining_symbols

    def test_eviction_does_not_remove_open_positions(self, store):
        """If all positions are open, no eviction occurs even at max size."""
        store._positions_max_size = 2

        # Fill 2 open positions
        store.on_fill(_make_fill(Side.BUY, qty=1, price=5_000_000, symbol="SYM_A"))
        store.on_fill(_make_fill(Side.BUY, qty=1, price=5_000_000, symbol="SYM_B"))

        # Third fill: eviction tries but no flat positions to evict
        store.on_fill(_make_fill(Side.BUY, qty=1, price=5_000_000, symbol="SYM_C"))

        # All 3 should still exist (eviction found nothing to remove, position added anyway)
        assert len(store.positions) == 3


# ===========================================================================
# 8. Portfolio drawdown
# ===========================================================================


class TestPortfolioDrawdown:
    """Process profitable fills -> drawdown=0.0, then loss fills -> drawdown > 0."""

    def test_no_fills_drawdown_zero(self, store):
        assert store.get_drawdown_pct() == 0.0

    def test_profit_then_loss_drawdown(self, store):
        # Profitable trade: buy 10 @ 500, sell 10 @ 510
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=5_100_000))
        # PnL = 1_000_000, at peak
        assert store.total_pnl == 1_000_000
        assert store.get_drawdown_pct() == 0.0

        # Losing trade: buy 10 @ 520, sell 10 @ 500
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_200_000))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=5_000_000))
        # PnL delta = (5_000_000 - 5_200_000) * 10 = -2_000_000
        # Total PnL = 1_000_000 - 2_000_000 = -1_000_000
        assert store.total_pnl == -1_000_000

        # Drawdown = (peak - current) / peak = (1_000_000 - (-1_000_000)) / 1_000_000 = 2.0
        assert store.get_drawdown_pct() == pytest.approx(2.0)

    def test_drawdown_partial_recovery(self, store):
        # Win big
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=5_200_000))
        # PnL = 2_000_000
        assert store.total_pnl == 2_000_000

        # Lose some
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_200_000))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=5_100_000))
        # PnL delta = -1_000_000, total = 1_000_000
        assert store.total_pnl == 1_000_000

        # Drawdown = (2_000_000 - 1_000_000) / 2_000_000 = 0.5
        assert store.get_drawdown_pct() == pytest.approx(0.5)

    def test_drawdown_returns_to_zero_at_new_peak(self, store):
        # Win
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=5_100_000))

        # Lose
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_200_000))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=5_100_000))
        assert store.get_drawdown_pct() > 0.0

        # Win big to surpass old peak
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=5_300_000))
        # Total PnL now exceeds old peak
        assert store.get_drawdown_pct() == 0.0


# ===========================================================================
# 9. Multi-symbol isolation
# ===========================================================================


class TestMultiSymbolIsolation:
    """Same strategy, different symbols, independent position tracking."""

    def test_independent_positions_per_symbol(self, store):
        # Buy symbol A
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000, symbol="SYM_A"))
        # Buy symbol B at different price
        store.on_fill(_make_fill(Side.BUY, qty=5, price=6_000_000, symbol="SYM_B"))

        assert len(store.positions) == 2

        positions_by_symbol = {pos.symbol: pos for pos in store.positions.values()}

        assert positions_by_symbol["SYM_A"].net_qty == 10
        assert positions_by_symbol["SYM_A"].avg_price_scaled == 5_000_000
        assert positions_by_symbol["SYM_B"].net_qty == 5
        assert positions_by_symbol["SYM_B"].avg_price_scaled == 6_000_000

    def test_closing_one_symbol_does_not_affect_other(self, store):
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000, symbol="SYM_A"))
        store.on_fill(_make_fill(Side.BUY, qty=5, price=6_000_000, symbol="SYM_B"))

        # Close SYM_A
        store.on_fill(_make_fill(Side.SELL, qty=10, price=5_100_000, symbol="SYM_A"))

        positions_by_symbol = {pos.symbol: pos for pos in store.positions.values()}

        assert positions_by_symbol["SYM_A"].net_qty == 0
        assert positions_by_symbol["SYM_A"].realized_pnl_scaled == 1_000_000
        # SYM_B unaffected
        assert positions_by_symbol["SYM_B"].net_qty == 5
        assert positions_by_symbol["SYM_B"].realized_pnl_scaled == 0

    def test_portfolio_pnl_aggregates_across_symbols(self, store):
        """Total PnL sums realized PnL from all symbols."""
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000, symbol="SYM_A"))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=5_100_000, symbol="SYM_A"))

        store.on_fill(_make_fill(Side.SELL, qty=5, price=6_000_000, symbol="SYM_B"))
        store.on_fill(_make_fill(Side.BUY, qty=5, price=5_800_000, symbol="SYM_B"))

        # SYM_A PnL = 1_000_000, SYM_B PnL = 1_000_000
        assert store.total_pnl == 2_000_000


# ===========================================================================
# 10. on_fill returns correct PositionDelta
# ===========================================================================


class TestOnFillReturnsPositionDelta:
    """Verify all fields of returned PositionDelta match position state."""

    def test_delta_fields_on_open(self, store):
        fill = _make_fill(
            Side.BUY,
            qty=10,
            price=5_000_000,
            account_id="ACC_X",
            strategy_id="STRAT_Y",
            symbol="SYM_Z",
        )
        delta = store.on_fill(fill)

        assert isinstance(delta, PositionDelta)
        assert delta.account_id == "ACC_X"
        assert delta.strategy_id == "STRAT_Y"
        assert delta.symbol == "SYM_Z"
        assert delta.net_qty == 10
        assert delta.avg_price == 5_000_000
        assert delta.realized_pnl == 0
        assert delta.unrealized_pnl == 0
        assert delta.delta_source == "FILL"

    def test_delta_fields_on_close(self, store):
        store.on_fill(
            _make_fill(
                Side.BUY,
                qty=10,
                price=5_000_000,
                account_id="ACC_X",
                strategy_id="STRAT_Y",
                symbol="SYM_Z",
            )
        )
        delta = store.on_fill(
            _make_fill(
                Side.SELL,
                qty=10,
                price=5_200_000,
                account_id="ACC_X",
                strategy_id="STRAT_Y",
                symbol="SYM_Z",
            )
        )

        assert delta.net_qty == 0
        assert delta.avg_price == 5_000_000
        assert delta.realized_pnl == 2_000_000
        assert delta.delta_source == "FILL"

    def test_delta_matches_internal_position_state(self, store):
        """Delta fields must mirror the internal Position object."""
        fill = _make_fill(Side.BUY, qty=7, price=4_500_000)
        delta = store.on_fill(fill)

        key = list(store.positions.keys())[0]
        pos = store.positions[key]

        assert delta.net_qty == pos.net_qty
        assert delta.avg_price == pos.avg_price_scaled
        assert delta.realized_pnl == pos.realized_pnl_scaled


# ===========================================================================
# 11. Zero-quantity edge case
# ===========================================================================


class TestZeroQuantityEdge:
    """net_qty=0 after full close: position exists but is flat."""

    def test_flat_position_exists_after_close(self, store):
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000))
        delta = store.on_fill(_make_fill(Side.SELL, qty=10, price=5_000_000))

        assert delta.net_qty == 0
        # Position still exists in store
        assert len(store.positions) == 1
        key = list(store.positions.keys())[0]
        pos = store.positions[key]
        assert pos.net_qty == 0
        assert pos.realized_pnl_scaled == 0  # Break-even

    def test_reopen_after_flat(self, store):
        """Can open new position on same key after going flat."""
        store.on_fill(_make_fill(Side.BUY, qty=10, price=5_000_000))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=5_100_000))

        # Reopen
        delta = store.on_fill(_make_fill(Side.SELL, qty=5, price=5_200_000))
        assert delta.net_qty == -5
        assert delta.avg_price == 5_200_000
        # Realized PnL carries forward from first round trip
        assert delta.realized_pnl == 1_000_000


# ===========================================================================
# Position dataclass unit tests
# ===========================================================================


class TestPositionUpdate:
    """Direct Position.update() method tests."""

    def test_update_from_flat_buy(self):
        pos = Position(account_id="a", strategy_id="s", symbol="X")
        fill = _make_fill(Side.BUY, qty=5, price=5_000_000)
        pos.update(fill)

        assert pos.net_qty == 5
        assert pos.avg_price_scaled == 5_000_000
        assert pos.realized_pnl_scaled == 0

    def test_update_from_flat_sell(self):
        pos = Position(account_id="a", strategy_id="s", symbol="X")
        fill = _make_fill(Side.SELL, qty=3, price=6_000_000)
        pos.update(fill)

        assert pos.net_qty == -3
        assert pos.avg_price_scaled == 6_000_000

    def test_backward_compat_properties(self):
        pos = Position(account_id="a", strategy_id="s", symbol="X")
        pos.avg_price_scaled = 5_000_000
        pos.realized_pnl_scaled = 100_000
        pos.fees_scaled = 500

        assert pos.avg_price == 5_000_000
        assert pos.realized_pnl == 100_000
        assert pos.fees == 500

    def test_descaled_properties(self):
        pos = Position(account_id="a", strategy_id="s", symbol="X")
        pos.avg_price_scaled = 5_000_000
        pos.realized_pnl_scaled = 1_000_000
        pos.fees_scaled = 10_000

        scale = 10_000
        assert pos.descaled_avg_price(scale) == pytest.approx(500.0)
        assert pos.descaled_realized_pnl(scale) == pytest.approx(100.0)
        assert pos.descaled_fees(scale) == pytest.approx(1.0)

    def test_descaled_with_zero_scale(self):
        pos = Position(account_id="a", strategy_id="s", symbol="X")
        pos.avg_price_scaled = 5_000_000
        assert pos.descaled_avg_price(0) == 0.0
        assert pos.descaled_realized_pnl(0) == 0.0
        assert pos.descaled_fees(0) == 0.0

    def test_last_update_ts_set(self):
        pos = Position(account_id="a", strategy_id="s", symbol="X")
        ts = 1_700_000_000_000_000_000
        fill = _make_fill(Side.BUY, qty=1, price=5_000_000, match_ts_ns=ts)
        pos.update(fill)
        assert pos.last_update_ts == ts
