"""Position conservation contract tests.

Verifies inventory conservation, PnL conservation on close, fee accumulation,
position flips, PnL sign correctness, and PositionStore eviction behavior.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

# Force Python position tracker
os.environ["HFT_RUST_POSITIONS"] = "0"

from hft_platform.contracts.execution import FillEvent, Side

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fill(
    *,
    side: Side = Side.BUY,
    qty: int = 1,
    price: int = 1_000_000,
    fee: int = 0,
    tax: int = 0,
    ts: int = 1_000_000_000,
    symbol: str = "2330",
    strategy_id: str = "test_strat",
    account_id: str = "acc1",
) -> FillEvent:
    return FillEvent(
        fill_id="f1",
        account_id=account_id,
        order_id="o1",
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


def _make_position_store():
    """Create a PositionStore with Rust and metrics disabled."""
    with patch("hft_platform.execution.positions.MetricsRegistry") as mock_mr:
        mock_mr.get.return_value = None
        from hft_platform.execution.positions import PositionStore

        store = PositionStore()
        store._rust_tracker = None
        store.metrics = None
        return store


# ---------------------------------------------------------------------------
# 1. Inventory conservation
# ---------------------------------------------------------------------------


class TestInventoryConservation:
    def test_net_qty_equals_buys_minus_sells(self):
        """net_qty = sum(buy_qty) - sum(sell_qty)."""
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=10, ts=1))
        store.on_fill(_make_fill(side=Side.BUY, qty=5, ts=2))
        delta = store.on_fill(_make_fill(side=Side.SELL, qty=3, ts=3))
        assert delta.net_qty == 10 + 5 - 3  # 12

    def test_net_qty_goes_negative(self):
        """Selling more than bought produces negative net_qty."""
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=2, ts=1))
        delta = store.on_fill(_make_fill(side=Side.SELL, qty=5, ts=2))
        assert delta.net_qty == 2 - 5  # -3


# ---------------------------------------------------------------------------
# 2. PnL conservation on full close
# ---------------------------------------------------------------------------


class TestPnlConservation:
    def test_full_close_pnl(self):
        """Full close PnL = (exit - entry) * qty for long."""
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=1_000_000, ts=1))
        delta = store.on_fill(_make_fill(side=Side.SELL, qty=10, price=1_050_000, ts=2))
        assert delta.net_qty == 0
        assert delta.realized_pnl == (1_050_000 - 1_000_000) * 10  # 500_000

    def test_full_close_short_pnl(self):
        """Full close PnL = (entry - exit) * qty for short."""
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=2_000_000, ts=1))
        delta = store.on_fill(_make_fill(side=Side.BUY, qty=10, price=1_950_000, ts=2))
        assert delta.net_qty == 0
        assert delta.realized_pnl == (2_000_000 - 1_950_000) * 10  # 500_000


# ---------------------------------------------------------------------------
# 3. Fee accumulation
# ---------------------------------------------------------------------------


class TestFeeAccumulation:
    def test_fees_sum_over_fills(self):
        """Total fees = sum(fee + tax) across all fills."""
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=1, fee=100, tax=50, ts=1))
        store.on_fill(_make_fill(side=Side.SELL, qty=1, fee=200, tax=80, ts=2))

        key = "acc1:test_strat:2330"
        pos = store.positions[key]
        assert pos.fees_scaled == (100 + 50) + (200 + 80)  # 430


# ---------------------------------------------------------------------------
# 4. Partial close PnL
# ---------------------------------------------------------------------------


class TestPartialClosePnl:
    def test_partial_close_realizes_proportional_pnl(self):
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=1_000_000, ts=1))
        delta = store.on_fill(_make_fill(side=Side.SELL, qty=4, price=1_100_000, ts=2))
        assert delta.net_qty == 6
        assert delta.realized_pnl == (1_100_000 - 1_000_000) * 4  # 400_000


# ---------------------------------------------------------------------------
# 5. Position flip: long -> short
# ---------------------------------------------------------------------------


class TestPositionFlipLongShort:
    def test_flip_long_to_short(self):
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=5, price=1_000_000, ts=1))
        delta = store.on_fill(_make_fill(side=Side.SELL, qty=8, price=1_200_000, ts=2))
        # Close 5 long, open 3 short
        assert delta.net_qty == -3
        assert delta.realized_pnl == (1_200_000 - 1_000_000) * 5  # 1_000_000

    def test_flip_avg_price_resets(self):
        """After flip, avg_price should be the new side's entry price."""
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=3, price=1_000_000, ts=1))
        store.on_fill(_make_fill(side=Side.SELL, qty=5, price=1_200_000, ts=2))

        key = "acc1:test_strat:2330"
        pos = store.positions[key]
        assert pos.net_qty == -2
        assert pos.avg_price_scaled == 1_200_000


# ---------------------------------------------------------------------------
# 6. Position flip: short -> long
# ---------------------------------------------------------------------------


class TestPositionFlipShortLong:
    def test_flip_short_to_long(self):
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.SELL, qty=4, price=2_000_000, ts=1))
        delta = store.on_fill(_make_fill(side=Side.BUY, qty=7, price=1_800_000, ts=2))
        # Close 4 short (profit), open 3 long
        assert delta.net_qty == 3
        assert delta.realized_pnl == (2_000_000 - 1_800_000) * 4  # 800_000


# ---------------------------------------------------------------------------
# 7. Zero-crossing avg_price
# ---------------------------------------------------------------------------


class TestZeroCrossingAvgPrice:
    def test_flat_then_reopen(self):
        """After closing to flat and reopening, avg_price = new fill price."""
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=5, price=1_000_000, ts=1))
        store.on_fill(_make_fill(side=Side.SELL, qty=5, price=1_100_000, ts=2))

        key = "acc1:test_strat:2330"
        pos = store.positions[key]
        assert pos.net_qty == 0

        # Reopen
        store.on_fill(_make_fill(side=Side.BUY, qty=3, price=2_000_000, ts=3))
        assert pos.avg_price_scaled == 2_000_000
        assert pos.net_qty == 3


# ---------------------------------------------------------------------------
# 8. Multiple opens avg_price
# ---------------------------------------------------------------------------


class TestMultipleOpensAvgPrice:
    def test_three_buys_weighted_average(self):
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=2, price=1_000_000, ts=1))
        store.on_fill(_make_fill(side=Side.BUY, qty=3, price=1_200_000, ts=2))
        store.on_fill(_make_fill(side=Side.BUY, qty=5, price=1_100_000, ts=3))

        key = "acc1:test_strat:2330"
        pos = store.positions[key]
        # Weighted: (2*1M + 3*1.2M + 5*1.1M) / 10 = (2M + 3.6M + 5.5M) / 10 = 11.1M / 10 = 1_110_000
        assert pos.avg_price_scaled == 1_110_000
        assert pos.net_qty == 10


# ---------------------------------------------------------------------------
# 9. Flat position reopen
# ---------------------------------------------------------------------------


class TestFlatPositionReopen:
    def test_reopen_after_flat(self):
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=1, price=1_000_000, ts=1))
        store.on_fill(_make_fill(side=Side.SELL, qty=1, price=1_000_000, ts=2))

        key = "acc1:test_strat:2330"
        pos = store.positions[key]
        assert pos.net_qty == 0

        store.on_fill(_make_fill(side=Side.SELL, qty=2, price=2_500_000, ts=3))
        assert pos.net_qty == -2
        assert pos.avg_price_scaled == 2_500_000


# ---------------------------------------------------------------------------
# 10. PnL sign: long profit / loss
# ---------------------------------------------------------------------------


class TestPnlSignLong:
    def test_long_profit(self):
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=1, price=1_000_000, ts=1))
        delta = store.on_fill(_make_fill(side=Side.SELL, qty=1, price=1_100_000, ts=2))
        assert delta.realized_pnl > 0

    def test_long_loss(self):
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=1, price=1_000_000, ts=1))
        delta = store.on_fill(_make_fill(side=Side.SELL, qty=1, price=900_000, ts=2))
        assert delta.realized_pnl < 0


# ---------------------------------------------------------------------------
# 11. PnL sign: short profit / loss
# ---------------------------------------------------------------------------


class TestPnlSignShort:
    def test_short_profit(self):
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.SELL, qty=1, price=1_000_000, ts=1))
        delta = store.on_fill(_make_fill(side=Side.BUY, qty=1, price=900_000, ts=2))
        assert delta.realized_pnl > 0

    def test_short_loss(self):
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.SELL, qty=1, price=1_000_000, ts=1))
        delta = store.on_fill(_make_fill(side=Side.BUY, qty=1, price=1_100_000, ts=2))
        assert delta.realized_pnl < 0


# ---------------------------------------------------------------------------
# 12. PositionStore eviction
# ---------------------------------------------------------------------------


class TestPositionStoreEviction:
    def test_eviction_at_max_size(self, monkeypatch):
        """PositionStore evicts flat positions when reaching max_size."""
        monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
        with patch("hft_platform.execution.positions.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = None
            from hft_platform.execution.positions import PositionStore

            store = PositionStore()
            store._rust_tracker = None
            store.metrics = None
            store._positions_max_size = 5

            # Fill 5 positions and close them (flat)
            for i in range(5):
                sym = f"SYM{i}"
                store.on_fill(_make_fill(side=Side.BUY, qty=1, price=1_000_000, ts=i * 2 + 1, symbol=sym))
                store.on_fill(_make_fill(side=Side.SELL, qty=1, price=1_000_000, ts=i * 2 + 2, symbol=sym))

            assert len(store.positions) == 5

            # Adding a 6th symbol triggers eviction of flat positions
            store.on_fill(_make_fill(side=Side.BUY, qty=1, price=1_000_000, ts=100, symbol="SYM_NEW"))
            assert len(store.positions) <= 5


# ---------------------------------------------------------------------------
# 13. PositionStore key format
# ---------------------------------------------------------------------------


class TestPositionStoreKeyFormat:
    def test_key_format(self):
        store = _make_position_store()
        store.on_fill(_make_fill(account_id="ACC", strategy_id="STRAT", symbol="SYM", ts=1))
        assert "ACC:STRAT:SYM" in store.positions


# ---------------------------------------------------------------------------
# 14. Hypothesis property tests
# ---------------------------------------------------------------------------

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False

    def given(*args, **kwargs):  # type: ignore[misc]
        def decorator(f):
            def wrapper(*a, **kw):
                pytest.skip("hypothesis not installed")

            return wrapper

        return decorator

    def settings(**kwargs):  # type: ignore[misc]
        def decorator(f):
            return f

        return decorator

    class _St:
        def integers(self, **kw):
            return None

        def lists(self, *a, **kw):
            return None

        def tuples(self, *a, **kw):
            return None

    st = _St()  # type: ignore[assignment]


class TestHypothesisPositionConservation:
    @settings(max_examples=50)
    @given(
        st.integers(min_value=1, max_value=500),  # buy qty
        st.integers(min_value=1, max_value=500),  # sell qty
    )
    def test_net_qty_conservation(self, buy_qty, sell_qty):
        """net_qty is always buy_qty - sell_qty regardless of prices."""
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=buy_qty, price=1_000_000, ts=1))
        delta = store.on_fill(_make_fill(side=Side.SELL, qty=sell_qty, price=2_000_000, ts=2))
        assert delta.net_qty == buy_qty - sell_qty

    @settings(max_examples=50)
    @given(
        st.integers(min_value=1, max_value=5000),  # fee
        st.integers(min_value=1, max_value=20),  # n fills
    )
    def test_fee_monotonicity(self, fee, n_fills):
        """Cumulative fees never decrease."""
        store = _make_position_store()
        key = "acc1:test_strat:2330"
        prev = 0
        for i in range(n_fills):
            store.on_fill(_make_fill(side=Side.BUY, qty=1, price=1_000_000, fee=fee, ts=i + 1))
            pos = store.positions[key]
            assert pos.fees_scaled >= prev
            prev = pos.fees_scaled

    @settings(max_examples=50)
    @given(
        st.integers(min_value=100_000, max_value=50_000_000),  # price
        st.integers(min_value=1, max_value=100),  # qty
    )
    def test_round_trip_pnl_zero(self, price, qty):
        """Buy and sell at same price => PnL is zero."""
        store = _make_position_store()
        store.on_fill(_make_fill(side=Side.BUY, qty=qty, price=price, ts=1))
        delta = store.on_fill(_make_fill(side=Side.SELL, qty=qty, price=price, ts=2))
        assert delta.realized_pnl == 0
        assert delta.net_qty == 0
