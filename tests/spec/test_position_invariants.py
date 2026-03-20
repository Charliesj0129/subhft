"""Property-based invariant tests for Position.update() logic.

Uses hypothesis to verify that for any sequence of fills netting to zero
quantity, the final net_qty is always zero.
"""

from __future__ import annotations

import random

import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
from hft_platform.execution.positions import Position

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRICE_MIN = 1_0000  # 1.0000 scaled
_PRICE_MAX = 100_000_0000  # 100,000.0000 scaled
_QTY_MIN = 1
_QTY_MAX = 100
_TS_BASE = 1_700_000_000_000_000_000  # arbitrary nanosecond epoch


def _make_fill(
    side: Side,
    qty: int,
    price: int,
    seq: int,
    fee: int = 0,
    tax: int = 0,
) -> FillEvent:
    return FillEvent(
        fill_id=f"f-{seq}",
        account_id="ACC",
        order_id=f"o-{seq}",
        strategy_id="strat-1",
        symbol="2330",
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=_TS_BASE + seq,
        match_ts_ns=_TS_BASE + seq,
    )


def _split_qty_into_parts(total: int, n: int, rng: random.Random) -> list[int]:
    """Split *total* into exactly *n* positive integer parts."""
    if n == 1:
        return [total]
    # Use a "stars and bars" approach: pick (n-1) cut points in [1, total-1].
    if total < n:
        raise ValueError("Cannot split total into n positive parts")
    cuts = sorted(rng.sample(range(1, total), n - 1))
    parts: list[int] = []
    prev = 0
    for c in cuts:
        parts.append(c - prev)
        prev = c
    parts.append(total - prev)
    return parts


# ---------------------------------------------------------------------------
# Hypothesis strategy: balanced fill sequences
# ---------------------------------------------------------------------------

if HYPOTHESIS_AVAILABLE:

    @st.composite
    def balanced_fill_sequences(draw):
        """Generate a shuffled list of fills where total buy qty == total sell qty."""
        n_buys = draw(st.integers(min_value=1, max_value=8))
        n_sells = draw(st.integers(min_value=1, max_value=8))
        total_qty = draw(st.integers(min_value=max(n_buys, n_sells), max_value=500))

        # Deterministic RNG seeded from hypothesis
        seed = draw(st.integers(min_value=0, max_value=2**31))
        rng = random.Random(seed)

        buy_qtys = _split_qty_into_parts(total_qty, n_buys, rng)
        sell_qtys = _split_qty_into_parts(total_qty, n_sells, rng)

        fills: list[tuple[Side, int, int]] = []
        for q in buy_qtys:
            price = draw(st.integers(min_value=_PRICE_MIN, max_value=_PRICE_MAX))
            fills.append((Side.BUY, q, price))
        for q in sell_qtys:
            price = draw(st.integers(min_value=_PRICE_MIN, max_value=_PRICE_MAX))
            fills.append((Side.SELL, q, price))

        # Shuffle order
        rng.shuffle(fills)
        return fills


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
class TestPositionInvariants:
    """Property-based tests for Position.update()."""

    @given(data=balanced_fill_sequences())
    @settings(max_examples=200, deadline=None)
    def test_net_zero_after_balanced_fills(self, data: list[tuple[Side, int, int]]):
        """For any fill sequence where total buy qty == total sell qty,
        the final net_qty must be zero."""
        pos = Position(account_id="ACC", strategy_id="strat-1", symbol="2330")
        for seq, (side, qty, price) in enumerate(data):
            fill = _make_fill(side, qty, price, seq)
            pos.update(fill)

        assert pos.net_qty == 0, f"Expected net_qty=0 after balanced fills, got {pos.net_qty}. Fills: {data}"

    @given(data=balanced_fill_sequences())
    @settings(max_examples=200, deadline=None)
    def test_fees_accumulate_correctly(self, data: list[tuple[Side, int, int]]):
        """Fees must equal the sum of all fill fees and taxes regardless of order."""
        pos = Position(account_id="ACC", strategy_id="strat-1", symbol="2330")
        expected_fees = 0
        for seq, (side, qty, price) in enumerate(data):
            fee = seq * 10  # deterministic fee per fill
            tax = seq * 5
            fill = _make_fill(side, qty, price, seq, fee=fee, tax=tax)
            expected_fees += fee + tax
            pos.update(fill)

        assert pos.fees_scaled == expected_fees

    @given(
        qty=st.integers(min_value=1, max_value=100),
        buy_price=st.integers(min_value=_PRICE_MIN, max_value=_PRICE_MAX),
        sell_price=st.integers(min_value=_PRICE_MIN, max_value=_PRICE_MAX),
    )
    @settings(max_examples=200, deadline=None)
    def test_round_trip_pnl_is_correct(self, qty: int, buy_price: int, sell_price: int):
        """Buy then sell same qty: realized PnL == (sell - buy) * qty."""
        pos = Position(account_id="ACC", strategy_id="strat-1", symbol="2330")
        pos.update(_make_fill(Side.BUY, qty, buy_price, 0))
        pos.update(_make_fill(Side.SELL, qty, sell_price, 1))

        expected_pnl = (sell_price - buy_price) * qty
        assert pos.realized_pnl_scaled == expected_pnl
        assert pos.net_qty == 0

    @given(
        qty=st.integers(min_value=1, max_value=100),
        sell_price=st.integers(min_value=_PRICE_MIN, max_value=_PRICE_MAX),
        buy_price=st.integers(min_value=_PRICE_MIN, max_value=_PRICE_MAX),
    )
    @settings(max_examples=200, deadline=None)
    def test_short_round_trip_pnl_is_correct(self, qty: int, sell_price: int, buy_price: int):
        """Short sell then buy cover: realized PnL == (sell - buy) * qty."""
        pos = Position(account_id="ACC", strategy_id="strat-1", symbol="2330")
        pos.update(_make_fill(Side.SELL, qty, sell_price, 0))
        pos.update(_make_fill(Side.BUY, qty, buy_price, 1))

        expected_pnl = (sell_price - buy_price) * qty
        assert pos.realized_pnl_scaled == expected_pnl
        assert pos.net_qty == 0

    @given(
        qty=st.integers(min_value=1, max_value=100),
        price=st.integers(min_value=_PRICE_MIN, max_value=_PRICE_MAX),
    )
    @settings(max_examples=200, deadline=None)
    def test_avg_price_on_flat_open_equals_fill_price(self, qty: int, price: int):
        """Opening from flat sets avg_price to the fill price exactly."""
        pos = Position(account_id="ACC", strategy_id="strat-1", symbol="2330")
        pos.update(_make_fill(Side.BUY, qty, price, 0))
        assert pos.avg_price_scaled == price
        assert pos.net_qty == qty

    @given(
        qty=st.integers(min_value=1, max_value=50),
        price=st.integers(min_value=_PRICE_MIN, max_value=_PRICE_MAX),
    )
    @settings(max_examples=200, deadline=None)
    def test_same_price_averaging_preserves_price(self, qty: int, price: int):
        """Adding to a position at the same price should not change avg_price."""
        pos = Position(account_id="ACC", strategy_id="strat-1", symbol="2330")
        pos.update(_make_fill(Side.BUY, qty, price, 0))
        pos.update(_make_fill(Side.BUY, qty, price, 1))
        assert pos.avg_price_scaled == price
        assert pos.net_qty == qty * 2

    @given(
        price=st.integers(min_value=_PRICE_MIN, max_value=_PRICE_MAX),
    )
    @settings(max_examples=200, deadline=None)
    def test_buy_sell_single_unit_zero_pnl(self, price: int):
        """Buy 1 and sell 1 at same price: zero PnL."""
        pos = Position(account_id="ACC", strategy_id="strat-1", symbol="2330")
        pos.update(_make_fill(Side.BUY, 1, price, 0))
        pos.update(_make_fill(Side.SELL, 1, price, 1))
        assert pos.net_qty == 0
        assert pos.realized_pnl_scaled == 0
