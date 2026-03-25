"""Unit tests for Position, PositionStore, and portfolio tracking."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent, PositionDelta, Side
from hft_platform.execution.positions import Position, PositionStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fill(
    *,
    side: Side = Side.BUY,
    qty: int = 10,
    price: int = 1000_0000,  # scaled x10000
    fee: int = 100,
    tax: int = 50,
    account_id: str = "acct1",
    strategy_id: str = "strat1",
    symbol: str = "2330",
    match_ts_ns: int = 1_000_000_000,
) -> FillEvent:
    return FillEvent(
        fill_id="F001",
        account_id=account_id,
        order_id="ORD001",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=match_ts_ns,
        match_ts_ns=match_ts_ns,
    )


@pytest.fixture(autouse=True)
def _disable_rust(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
    monkeypatch.setattr(
        "hft_platform.observability.metrics.MetricsRegistry.get",
        staticmethod(lambda: None),
    )


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> PositionStore:
    # Mock SymbolMetadata and PriceCodec to avoid file IO
    mock_metadata = MagicMock()
    mock_metadata.price_scale.return_value = 10_000
    mock_metadata.contract_multiplier.return_value = 1
    monkeypatch.setattr(
        "hft_platform.execution.positions.SymbolMetadata",
        lambda *a, **kw: mock_metadata,
    )
    mock_provider = MagicMock()
    mock_provider.price_scale.return_value = 10_000
    monkeypatch.setattr(
        "hft_platform.execution.positions.SymbolMetadataPriceScaleProvider",
        lambda *a, **kw: mock_provider,
    )
    mock_codec = MagicMock()
    mock_codec.scale_factor.return_value = 10_000
    monkeypatch.setattr(
        "hft_platform.execution.positions.PriceCodec",
        lambda *a, **kw: mock_codec,
    )
    s = PositionStore()
    s._rust_tracker = None
    s.metrics = None
    return s


# ---------------------------------------------------------------------------
# Position: open / add long
# ---------------------------------------------------------------------------


class TestPositionLong:
    def test_open_long(self) -> None:
        pos = Position("a", "s", "2330")
        fill = _make_fill(side=Side.BUY, qty=10, price=1000_0000)
        pos.update(fill)
        assert pos.net_qty == 10
        assert pos.avg_price_scaled == 1000_0000
        # fee=100 + tax=50 = 150 deducted at execution time
        assert pos.realized_pnl_scaled == -150

    def test_add_long(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.BUY, qty=10, price=1000_0000))
        pos.update(_make_fill(side=Side.BUY, qty=10, price=1100_0000))
        assert pos.net_qty == 20
        # Weighted avg: (10*1000_0000 + 10*1100_0000) / 20 = 1050_0000
        assert pos.avg_price_scaled == 1050_0000


# ---------------------------------------------------------------------------
# Position: partial / full close long
# ---------------------------------------------------------------------------


class TestPositionCloseLong:
    def test_partial_close_long(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.BUY, qty=10, price=1000_0000))
        pos.update(_make_fill(side=Side.SELL, qty=5, price=1100_0000))
        assert pos.net_qty == 5
        # Gross PnL = (1100_0000 - 1000_0000) * 5 = 500_0000
        # Fees: 2 fills * (fee=100 + tax=50) = 300
        # Net PnL = 500_0000 - 300 = 499_9700
        assert pos.realized_pnl_scaled == 499_9700

    def test_full_close_long(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.BUY, qty=10, price=1000_0000))
        pos.update(_make_fill(side=Side.SELL, qty=10, price=1200_0000))
        assert pos.net_qty == 0
        # Gross PnL = (1200-1000)*10 scaled = 2000_0000
        # Fees: 2 fills * 150 = 300
        # Net PnL = 2000_0000 - 300 = 1_9999_700
        assert pos.realized_pnl_scaled == 1_9999_700


# ---------------------------------------------------------------------------
# Position: open / add short
# ---------------------------------------------------------------------------


class TestPositionShort:
    def test_open_short(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.SELL, qty=10, price=1000_0000))
        assert pos.net_qty == -10
        assert pos.avg_price_scaled == 1000_0000

    def test_add_short(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.SELL, qty=10, price=1000_0000))
        pos.update(_make_fill(side=Side.SELL, qty=10, price=900_0000))
        assert pos.net_qty == -20
        # Weighted avg: (-10*1000_0000 + -10*900_0000) / -20 = 950_0000
        assert pos.avg_price_scaled == 950_0000


# ---------------------------------------------------------------------------
# Position: partial / full close short
# ---------------------------------------------------------------------------


class TestPositionCloseShort:
    def test_partial_close_short(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.SELL, qty=10, price=1000_0000))
        pos.update(_make_fill(side=Side.BUY, qty=5, price=900_0000))
        assert pos.net_qty == -5
        # Gross PnL = (1000_0000 - 900_0000) * 5 = 500_0000
        # Fees: 2 fills * 150 = 300
        # Net PnL = 500_0000 - 300 = 499_9700
        assert pos.realized_pnl_scaled == 499_9700

    def test_full_close_short(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.SELL, qty=10, price=1000_0000))
        pos.update(_make_fill(side=Side.BUY, qty=10, price=800_0000))
        assert pos.net_qty == 0
        # Gross PnL = (1000-800)*10 scaled = 2000_0000
        # Fees: 2 fills * 150 = 300
        # Net PnL = 2000_0000 - 300 = 1_9999_700
        assert pos.realized_pnl_scaled == 1_9999_700


# ---------------------------------------------------------------------------
# Position: flip long -> short, short -> long
# ---------------------------------------------------------------------------


class TestPositionFlip:
    def test_flip_long_to_short(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.BUY, qty=5, price=1000_0000))
        pos.update(_make_fill(side=Side.SELL, qty=10, price=1100_0000))
        assert pos.net_qty == -5
        # Gross close PnL: (1100_0000 - 1000_0000) * 5 = 500_0000
        # Fees: 2 fills * 150 = 300
        # Net PnL = 500_0000 - 300 = 499_9700
        assert pos.realized_pnl_scaled == 499_9700
        # New avg_price for the short side
        assert pos.avg_price_scaled == 1100_0000

    def test_flip_short_to_long(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.SELL, qty=5, price=1000_0000))
        pos.update(_make_fill(side=Side.BUY, qty=10, price=900_0000))
        assert pos.net_qty == 5
        # Gross close PnL: (1000_0000 - 900_0000) * 5 = 500_0000
        # Fees: 2 fills * 150 = 300
        # Net PnL = 500_0000 - 300 = 499_9700
        assert pos.realized_pnl_scaled == 499_9700
        assert pos.avg_price_scaled == 900_0000


# ---------------------------------------------------------------------------
# Fee / tax accumulation
# ---------------------------------------------------------------------------


class TestFeeAccumulation:
    def test_fees_accumulate(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(fee=100, tax=50))
        pos.update(_make_fill(fee=200, tax=75))
        assert pos.fees_scaled == 425  # 100+50+200+75


# ---------------------------------------------------------------------------
# last_update_ts
# ---------------------------------------------------------------------------


class TestLastUpdateTs:
    def test_last_update_ts_set(self) -> None:
        pos = Position("a", "s", "2330")
        fill = _make_fill(match_ts_ns=999_000)
        pos.update(fill)
        assert pos.last_update_ts == 999_000


# ---------------------------------------------------------------------------
# PositionStore: key format
# ---------------------------------------------------------------------------


class TestPositionStoreKey:
    def test_key_format(self, store: PositionStore) -> None:
        assert store._key("acct1", "strat1", "2330") == "acct1:strat1:2330"


# ---------------------------------------------------------------------------
# PositionStore: on_fill creates position, returns PositionDelta
# ---------------------------------------------------------------------------


class TestPositionStoreOnFill:
    def test_on_fill_creates_position(self, store: PositionStore) -> None:
        fill = _make_fill()
        delta = store.on_fill(fill)
        assert isinstance(delta, PositionDelta)
        assert delta.net_qty == 10
        assert delta.delta_source == "FILL"
        assert "acct1:strat1:2330" in store.positions


# ---------------------------------------------------------------------------
# PositionStore: eviction at max_size, preserves active
# ---------------------------------------------------------------------------


class TestPositionStoreEviction:
    def test_eviction_at_max_size(self, store: PositionStore) -> None:
        store._positions_max_size = 3

        # Create 3 flat positions
        for i in range(3):
            fill = _make_fill(symbol=f"SYM{i}", match_ts_ns=i * 1000)
            store.on_fill(fill)
            # Close them (make flat)
            close = _make_fill(side=Side.SELL, symbol=f"SYM{i}", match_ts_ns=i * 1000 + 1)
            store.on_fill(close)

        assert len(store.positions) == 3  # all flat, at limit

        # Adding a new one should trigger eviction of some flat positions
        new_fill = _make_fill(symbol="NEW1")
        store.on_fill(new_fill)
        # Should have evicted at least one flat position
        assert len(store.positions) <= 3

    def test_preserves_active_positions(self, store: PositionStore) -> None:
        store._positions_max_size = 2

        # One active position
        store.on_fill(_make_fill(symbol="ACTIVE"))

        # One flat position
        store.on_fill(_make_fill(symbol="FLAT"))
        store.on_fill(_make_fill(side=Side.SELL, symbol="FLAT"))

        # Trigger eviction
        store.on_fill(_make_fill(symbol="NEW"))

        assert "acct1:strat1:ACTIVE" in store.positions
        assert "acct1:strat1:NEW" in store.positions


# ---------------------------------------------------------------------------
# PositionStore: on_fill_async wrapper
# ---------------------------------------------------------------------------


class TestOnFillAsync:
    @pytest.mark.asyncio
    async def test_on_fill_async_returns_delta(self, store: PositionStore) -> None:
        fill = _make_fill()
        delta = await store.on_fill_async(fill)
        assert isinstance(delta, PositionDelta)
        assert delta.net_qty == 10


# ---------------------------------------------------------------------------
# Portfolio: total PnL, peak equity, drawdown
# ---------------------------------------------------------------------------


class TestPortfolio:
    def test_total_pnl_tracking(self, store: PositionStore) -> None:
        # Open and close with profit
        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=1000_0000, symbol="A"))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=1100_0000, symbol="A"))
        # Gross PnL = (1100-1000)*10 = 1000_0000; Fees = 2*150 = 300; Net = 999_9700
        assert store.total_pnl == 999_9700

    def test_peak_equity_watermark(self, store: PositionStore) -> None:
        # First profitable trade
        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=1000_0000, symbol="A"))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=1100_0000, symbol="A"))
        peak1 = store._peak_equity_scaled
        # Net PnL = 1000_0000 - 300 = 999_9700
        assert peak1 == 999_9700

        # Losing trade
        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=1200_0000, symbol="B"))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=1100_0000, symbol="B"))
        # Peak should not decrease
        assert store._peak_equity_scaled == peak1

    def test_drawdown_pct(self, store: PositionStore) -> None:
        # Profit first
        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=1000_0000, symbol="A"))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=1100_0000, symbol="A"))
        assert store.get_drawdown_pct() == 0.0  # at peak

        # Loss: gross = -(1200-1100)*10 = -1000_0000, fees = 300, net = -1000_0300
        # total net pnl = 999_9700 + (-1000_0300) = -600
        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=1200_0000, symbol="B"))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=1100_0000, symbol="B"))
        # total_pnl went negative; peak was 999_9700; drawdown > 1.0 clamped behavior:
        # (999_9700 - (-600)) / 999_9700 > 1.0 but the impl returns ratio
        assert store.get_drawdown_pct() > 0.0

    def test_drawdown_pct_no_peak(self, store: PositionStore) -> None:
        # No trades yet
        assert store.get_drawdown_pct() == 0.0
