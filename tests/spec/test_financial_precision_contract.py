"""Financial precision contract tests.

Verifies that the platform never uses float for financial calculations,
that scaled-integer arithmetic produces exact results, and that position
PnL tracking has zero drift.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Force Python position tracker (no Rust dependency in unit tests)
os.environ["HFT_RUST_POSITIONS"] = "0"

from hft_platform.contracts.execution import FillEvent, Side
from tests.factories.intents import make_order_intent

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


# TODO: migrate to tests.factories.components.make_risk_engine when available
def _make_risk_engine(tmp_path):
    """Create a RiskEngine with mocked dependencies."""
    cfg = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "tick_size": 0.01,
            "price_band_ticks": 200,
            "max_notional": 10_000_000,
            "per_symbol_max_notional": 50_000_000,
            "max_position_lots": 100,
            "daily_loss_limit": 500_000,
        },
        "strategies": {},
    }
    cfg_path = tmp_path / "strategy_limits.yaml"
    cfg_path.write_text(yaml.dump(cfg))

    with (
        patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
        patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
        patch("hft_platform.risk.engine.get_audit_writer", return_value=MagicMock()),
    ):
        mock_mr.get.return_value = None
        mock_lr.get.return_value = None
        from hft_platform.risk.engine import RiskEngine

        engine = RiskEngine(str(cfg_path), asyncio.Queue(), asyncio.Queue())
        engine.metrics = None
        return engine


def _make_position_store():
    """Create a PositionStore with mocked dependencies."""
    with patch("hft_platform.execution.positions.MetricsRegistry") as mock_mr:
        mock_mr.get.return_value = None
        from hft_platform.execution.positions import PositionStore

        store = PositionStore()
        store._rust_tracker = None
        store.metrics = None
        return store


# ---------------------------------------------------------------------------
# 1. Float rejection at risk boundary
# ---------------------------------------------------------------------------


class TestFloatRejection:
    def test_float_price_rejected_by_risk_engine(self, tmp_path):
        engine = _make_risk_engine(tmp_path)
        intent = make_order_intent(price=100.5)  # type: ignore[arg-type]  # float price
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "FLOAT_PRICE"

    def test_int_price_not_rejected_as_float(self, tmp_path):
        engine = _make_risk_engine(tmp_path)
        intent = make_order_intent(price=1_000_000)
        decision = engine.evaluate(intent)
        # Should not be rejected for FLOAT_PRICE
        assert decision.reason_code != "FLOAT_PRICE"


# ---------------------------------------------------------------------------
# 2. Scaled int round-trip
# ---------------------------------------------------------------------------


class TestScaledIntRoundTrip:
    def test_buy_sell_pnl_exact(self):
        """Buy 10 @ 1_000_000, sell 10 @ 1_010_000 => PnL exactly 100_000."""
        store = _make_position_store()
        buy = _make_fill(side=Side.BUY, qty=10, price=1_000_000, ts=1)
        store.on_fill(buy)

        sell = _make_fill(side=Side.SELL, qty=10, price=1_010_000, ts=2)
        delta = store.on_fill(sell)

        expected_pnl = (1_010_000 - 1_000_000) * 10  # 100_000
        assert delta.realized_pnl == expected_pnl
        assert delta.realized_pnl == 100_000
        assert delta.net_qty == 0

    def test_short_sell_buy_pnl_exact(self):
        """Short sell 5 @ 2_000_000, buy back 5 @ 1_990_000 => PnL exactly 50_000."""
        store = _make_position_store()
        sell = _make_fill(side=Side.SELL, qty=5, price=2_000_000, ts=1)
        store.on_fill(sell)

        buy = _make_fill(side=Side.BUY, qty=5, price=1_990_000, ts=2)
        delta = store.on_fill(buy)

        expected_pnl = (2_000_000 - 1_990_000) * 5  # 50_000
        assert delta.realized_pnl == expected_pnl
        assert delta.realized_pnl == 50_000


# ---------------------------------------------------------------------------
# 3. Position PnL is exact integer (no float drift)
# ---------------------------------------------------------------------------


class TestPositionPnlNoDrift:
    def test_multiple_fills_exact_integer(self):
        """After many fills, PnL remains an exact integer with no float drift."""
        store = _make_position_store()
        total_pnl = 0
        for i in range(100):
            buy = _make_fill(side=Side.BUY, qty=1, price=1_000_000, ts=i * 2 + 1)
            store.on_fill(buy)
            sell = _make_fill(side=Side.SELL, qty=1, price=1_000_100, ts=i * 2 + 2)
            delta = store.on_fill(sell)
            total_pnl += 100  # each round-trip: 100 scaled units

        assert delta.realized_pnl == total_pnl
        assert isinstance(delta.realized_pnl, int)


# ---------------------------------------------------------------------------
# 4. Normalizer output type contract
# ---------------------------------------------------------------------------


class TestNormalizerOutputType:
    def test_tick_price_is_int(self):
        """MarketDataNormalizer must produce int prices."""
        with (
            patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr,
        ):
            mock_mr.get.return_value = None
            from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

            norm = MarketDataNormalizer()
            payload = {"code": "2330", "close": 100.5, "volume": 10, "ts": 1_000_000_000}
            result = norm.normalize_tick(payload)
            if result is not None:
                if isinstance(result, tuple):
                    price = result[2]
                else:
                    price = result.price
                assert isinstance(price, int), f"Normalizer price must be int, got {type(price)}"


# ---------------------------------------------------------------------------
# 5. Edge cases: zero price, large price, fee accumulation
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_zero_price_fill(self):
        """Zero price fill should not cause division or drift errors."""
        store = _make_position_store()
        fill = _make_fill(side=Side.BUY, qty=1, price=0, ts=1)
        delta = store.on_fill(fill)
        assert delta.net_qty == 1
        assert delta.avg_price == 0

    def test_large_price_scale(self):
        """Large scaled prices should not overflow in Python (arbitrary precision)."""
        store = _make_position_store()
        large_price = 999_999_999_999  # ~100M NTD x10000
        buy = _make_fill(side=Side.BUY, qty=1000, price=large_price, ts=1)
        store.on_fill(buy)
        sell = _make_fill(side=Side.SELL, qty=1000, price=large_price + 1, ts=2)
        delta = store.on_fill(sell)
        assert delta.realized_pnl == 1000  # (large_price+1 - large_price) * 1000

    def test_fee_accumulation_exact(self):
        """Fees accumulate exactly over multiple fills."""
        store = _make_position_store()
        total_fees = 0
        for i in range(50):
            fill = _make_fill(side=Side.BUY, qty=1, price=1_000_000, fee=100, tax=50, ts=i + 1)
            store.on_fill(fill)
            total_fees += 150

        key = "acc1:test_strat:2330"
        pos = store.positions[key]
        assert pos.fees_scaled == total_fees
        assert pos.fees_scaled == 7500


# ---------------------------------------------------------------------------
# 6. Avg price weighted average
# ---------------------------------------------------------------------------


class TestAvgPrice:
    def test_weighted_average_two_buys(self):
        """Avg price is weighted average of two buy fills."""
        store = _make_position_store()
        buy1 = _make_fill(side=Side.BUY, qty=2, price=1_000_000, ts=1)
        store.on_fill(buy1)
        buy2 = _make_fill(side=Side.BUY, qty=3, price=1_100_000, ts=2)
        store.on_fill(buy2)

        key = "acc1:test_strat:2330"
        pos = store.positions[key]
        # Weighted avg = (2*1_000_000 + 3*1_100_000) / 5 = 5_300_000 / 5 = 1_060_000
        assert pos.avg_price_scaled == 1_060_000
        assert pos.net_qty == 5


# ---------------------------------------------------------------------------
# 7. Multiple partial closes
# ---------------------------------------------------------------------------


class TestPartialCloses:
    def test_partial_close_preserves_remainder(self):
        """Partial close realizes PnL proportionally, leaves remainder."""
        store = _make_position_store()
        buy = _make_fill(side=Side.BUY, qty=10, price=1_000_000, ts=1)
        store.on_fill(buy)

        sell = _make_fill(side=Side.SELL, qty=3, price=1_050_000, ts=2)
        delta = store.on_fill(sell)
        assert delta.net_qty == 7
        assert delta.realized_pnl == (1_050_000 - 1_000_000) * 3  # 150_000

    def test_two_partial_closes_cumulative_pnl(self):
        """Two partial closes accumulate PnL correctly."""
        store = _make_position_store()
        buy = _make_fill(side=Side.BUY, qty=10, price=1_000_000, ts=1)
        store.on_fill(buy)

        sell1 = _make_fill(side=Side.SELL, qty=4, price=1_020_000, ts=2)
        store.on_fill(sell1)
        sell2 = _make_fill(side=Side.SELL, qty=6, price=1_030_000, ts=3)
        delta = store.on_fill(sell2)

        expected = (1_020_000 - 1_000_000) * 4 + (1_030_000 - 1_000_000) * 6
        assert delta.realized_pnl == expected
        assert delta.net_qty == 0


# ---------------------------------------------------------------------------
# 8. Position flip PnL
# ---------------------------------------------------------------------------


class TestPositionFlip:
    def test_long_to_short_flip(self):
        """Flipping from long to short realizes full close PnL."""
        store = _make_position_store()
        buy = _make_fill(side=Side.BUY, qty=5, price=1_000_000, ts=1)
        store.on_fill(buy)

        sell = _make_fill(side=Side.SELL, qty=8, price=1_100_000, ts=2)
        delta = store.on_fill(sell)

        # Close 5 long @ profit, open 3 short
        assert delta.net_qty == -3
        assert delta.realized_pnl == (1_100_000 - 1_000_000) * 5  # 500_000


# ---------------------------------------------------------------------------
# 9. Hypothesis property tests
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


class TestHypothesisPrecision:
    @settings(max_examples=50)
    @given(st.integers(min_value=1, max_value=10_000_000_000))
    def test_price_scale_round_trip(self, price_raw):
        """Scaling and descaling a price preserves the original value."""
        scale = 10_000
        scaled = price_raw * scale
        descaled = scaled // scale
        assert descaled == price_raw

    @settings(max_examples=50)
    @given(
        st.integers(min_value=100_000, max_value=50_000_000),  # buy price
        st.integers(min_value=100_000, max_value=50_000_000),  # sell price
        st.integers(min_value=1, max_value=1000),  # qty
    )
    def test_pnl_sign_invariant(self, buy_price, sell_price, qty):
        """Long PnL sign: positive iff sell > buy."""
        store = _make_position_store()
        buy = _make_fill(side=Side.BUY, qty=qty, price=buy_price, ts=1)
        store.on_fill(buy)
        sell = _make_fill(side=Side.SELL, qty=qty, price=sell_price, ts=2)
        delta = store.on_fill(sell)

        expected_pnl = (sell_price - buy_price) * qty
        assert delta.realized_pnl == expected_pnl
        if sell_price > buy_price:
            assert delta.realized_pnl > 0
        elif sell_price < buy_price:
            assert delta.realized_pnl < 0
        else:
            assert delta.realized_pnl == 0

    @settings(max_examples=50)
    @given(
        st.integers(min_value=1, max_value=5000),  # fee per fill
        st.integers(min_value=1, max_value=20),  # number of fills
    )
    def test_fee_monotonicity(self, fee_per_fill, n_fills):
        """Fees are monotonically non-decreasing."""
        store = _make_position_store()
        key = "acc1:test_strat:2330"
        prev_fees = 0
        for i in range(n_fills):
            fill = _make_fill(side=Side.BUY, qty=1, price=1_000_000, fee=fee_per_fill, tax=0, ts=i + 1)
            store.on_fill(fill)
            pos = store.positions[key]
            assert pos.fees_scaled >= prev_fees
            prev_fees = pos.fees_scaled
        assert store.positions[key].fees_scaled == fee_per_fill * n_fills

    @settings(max_examples=50)
    @given(
        st.integers(min_value=1, max_value=100),  # buy qty
        st.integers(min_value=1, max_value=100),  # sell qty
    )
    def test_position_conservation(self, buy_qty, sell_qty):
        """net_qty = total_buys - total_sells."""
        store = _make_position_store()
        buy = _make_fill(side=Side.BUY, qty=buy_qty, price=1_000_000, ts=1)
        store.on_fill(buy)
        sell = _make_fill(side=Side.SELL, qty=sell_qty, price=1_000_000, ts=2)
        delta = store.on_fill(sell)
        assert delta.net_qty == buy_qty - sell_qty
