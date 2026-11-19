"""Unit tests for Position, PositionStore, and portfolio tracking."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent, PositionDelta, Side
from hft_platform.execution.positions import Position, PositionStore


def _make_fill(
    *,
    side: Side = Side.BUY,
    qty: int = 10,
    price: int = 1000_0000,
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
    import hft_platform.execution.positions as _positions

    monkeypatch.setattr(_positions, "_MIN_PEAK_SCALED", 2_000_000)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> PositionStore:
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


class TestPositionLong:
    def test_open_long(self) -> None:
        pos = Position("a", "s", "2330")
        fill = _make_fill(side=Side.BUY, qty=10, price=1000_0000)
        pos.update(fill)
        assert pos.net_qty == 10
        assert pos.avg_price_scaled == 1000_0000
        assert pos.realized_pnl_scaled == 0

    def test_add_long(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.BUY, qty=10, price=1000_0000))
        pos.update(_make_fill(side=Side.BUY, qty=10, price=1100_0000))
        assert pos.net_qty == 20
        assert pos.avg_price_scaled == 1050_0000


class TestPositionCloseLong:
    def test_partial_close_long(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.BUY, qty=10, price=1000_0000))
        pos.update(_make_fill(side=Side.SELL, qty=5, price=1100_0000))
        assert pos.net_qty == 5
        assert pos.realized_pnl_scaled == 500_0000

    def test_full_close_long(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.BUY, qty=10, price=1000_0000))
        pos.update(_make_fill(side=Side.SELL, qty=10, price=1200_0000))
        assert pos.net_qty == 0
        assert pos.realized_pnl_scaled == 2000_0000


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
        assert pos.avg_price_scaled == 950_0000


class TestPositionCloseShort:
    def test_partial_close_short(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.SELL, qty=10, price=1000_0000))
        pos.update(_make_fill(side=Side.BUY, qty=5, price=900_0000))
        assert pos.net_qty == -5
        assert pos.realized_pnl_scaled == 500_0000

    def test_full_close_short(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.SELL, qty=10, price=1000_0000))
        pos.update(_make_fill(side=Side.BUY, qty=10, price=800_0000))
        assert pos.net_qty == 0
        assert pos.realized_pnl_scaled == 2000_0000


class TestPositionFlip:
    def test_flip_long_to_short(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.BUY, qty=5, price=1000_0000))
        pos.update(_make_fill(side=Side.SELL, qty=10, price=1100_0000))
        assert pos.net_qty == -5
        assert pos.realized_pnl_scaled == 500_0000
        assert pos.avg_price_scaled == 1100_0000

    def test_flip_short_to_long(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(side=Side.SELL, qty=5, price=1000_0000))
        pos.update(_make_fill(side=Side.BUY, qty=10, price=900_0000))
        assert pos.net_qty == 5
        assert pos.realized_pnl_scaled == 500_0000
        assert pos.avg_price_scaled == 900_0000


class TestFeeAccumulation:
    def test_fees_accumulate(self) -> None:
        pos = Position("a", "s", "2330")
        pos.update(_make_fill(fee=150, tax=50))
        pos.update(_make_fill(fee=275, tax=75))
        assert pos.fees_scaled == 425


class TestLastUpdateTs:
    def test_last_update_ts_set(self) -> None:
        pos = Position("a", "s", "2330")
        fill = _make_fill(match_ts_ns=999_000)
        pos.update(fill)
        assert pos.last_update_ts == 999_000


class TestPositionStoreKey:
    def test_key_format(self, store: PositionStore) -> None:
        assert store._key("acct1", "strat1", "2330") == "acct1:strat1:2330"


class TestPositionStoreOnFill:
    def test_on_fill_creates_position(self, store: PositionStore) -> None:
        fill = _make_fill()
        delta = store.on_fill(fill)
        assert isinstance(delta, PositionDelta)
        assert delta.net_qty == 10
        assert delta.delta_source == "FILL"
        assert "acct1:strat1:2330" in store.positions


class TestPositionStoreEviction:
    def test_eviction_at_max_size(self, store: PositionStore) -> None:
        store._positions_max_size = 3

        for i in range(3):
            fill = _make_fill(symbol=f"SYM{i}", match_ts_ns=i * 1000)
            store.on_fill(fill)
            close = _make_fill(side=Side.SELL, symbol=f"SYM{i}", match_ts_ns=i * 1000 + 1)
            store.on_fill(close)

        assert len(store.positions) == 3

        new_fill = _make_fill(symbol="NEW1")
        store.on_fill(new_fill)
        assert len(store.positions) <= 3

    def test_preserves_active_positions(self, store: PositionStore) -> None:
        store._positions_max_size = 2

        store.on_fill(_make_fill(symbol="ACTIVE"))

        store.on_fill(_make_fill(symbol="FLAT"))
        store.on_fill(_make_fill(side=Side.SELL, symbol="FLAT"))

        store.on_fill(_make_fill(symbol="NEW"))

        assert "acct1:strat1:ACTIVE" in store.positions
        assert "acct1:strat1:NEW" in store.positions


class TestOnFillAsync:
    @pytest.mark.asyncio
    async def test_on_fill_async_returns_delta(self, store: PositionStore) -> None:
        fill = _make_fill()
        delta = await store.on_fill_async(fill)
        assert isinstance(delta, PositionDelta)
        assert delta.net_qty == 10


class TestPortfolio:
    def test_total_pnl_tracking(self, store: PositionStore) -> None:
        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=1000_0000, symbol="A"))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=1100_0000, symbol="A"))
        assert store.total_pnl == 1000_0000

    def test_peak_equity_watermark(self, store: PositionStore) -> None:
        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=1000_0000, symbol="A"))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=1100_0000, symbol="A"))
        peak1 = store._peak_equity_scaled
        assert peak1 == 1000_0000

        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=1200_0000, symbol="B"))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=1100_0000, symbol="B"))
        assert store._peak_equity_scaled == peak1

    def test_drawdown_pct(self, store: PositionStore) -> None:
        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=1000_0000, symbol="A"))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=1100_0000, symbol="A"))
        assert store.get_drawdown_pct() == 0.0

        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=1200_0000, symbol="B"))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=1100_0000, symbol="B"))
        assert store.get_drawdown_pct() == 1.0

    def test_drawdown_pct_no_peak(self, store: PositionStore) -> None:
        assert store.get_drawdown_pct() == 0.0
