"""Comprehensive position store tests: Python path parity, eviction, drawdown, edge cases.

Tests cover:
- Position.update() accumulation and PnL math (scaled int x10000)
- PositionStore drawdown tracking (peak equity, drawdown %)
- Double-fill (no dedup) behavior
- Position flipping (long -> short in single fill)
- Zero-quantity fill edge case
- Eviction of flat positions at capacity
- Rust/Python parity (when Rust is available)
"""

from __future__ import annotations

import pytest

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.core import timebase

_FILL_SEQ = 0


def _make_fill(
    *,
    side: Side,
    qty: int,
    price: int,
    fee: int = 0,
    tax: int = 0,
    account_id: str = "ACC1",
    strategy_id: str = "STRAT1",
    symbol: str = "2330",
    order_id: str = "ORD1",
) -> FillEvent:
    global _FILL_SEQ
    _FILL_SEQ += 1
    ts = timebase.now_ns()
    return FillEvent(
        fill_id=f"F{_FILL_SEQ}",
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


class TestPositionUpdate:
    """Verify Position.update() arithmetic using scaled-int prices (x10000)."""

    def test_buy_accumulate_and_sell_close(self) -> None:
        from hft_platform.execution.positions import Position

        pos = Position(account_id="ACC1", strategy_id="STRAT1", symbol="2330")

        fill1 = _make_fill(side=Side.BUY, qty=10, price=500_0000)
        pos.update(fill1)
        assert pos.net_qty == 10
        assert pos.avg_price_scaled == 500_0000
        assert pos.realized_pnl_scaled == 0

        fill2 = _make_fill(side=Side.BUY, qty=5, price=510_0000)
        pos.update(fill2)
        assert pos.net_qty == 15
        expected_avg = (10 * 500_0000 + 5 * 510_0000) // 15
        assert pos.avg_price_scaled == expected_avg
        assert pos.realized_pnl_scaled == 0

        fill3 = _make_fill(side=Side.SELL, qty=10, price=520_0000)
        pos.update(fill3)
        assert pos.net_qty == 5
        expected_pnl = (520_0000 - expected_avg) * 10
        assert pos.realized_pnl_scaled == expected_pnl
        assert expected_pnl > 0
        assert pos.avg_price_scaled == expected_avg

        fill4 = _make_fill(side=Side.SELL, qty=5, price=520_0000)
        pos.update(fill4)
        assert pos.net_qty == 0
        total_pnl = expected_pnl + (520_0000 - expected_avg) * 5
        assert pos.realized_pnl_scaled == total_pnl
        assert total_pnl > 0

    def test_fees_accumulate(self) -> None:
        from hft_platform.execution.positions import Position

        pos = Position(account_id="ACC1", strategy_id="STRAT1", symbol="2330")
        fill1 = _make_fill(side=Side.BUY, qty=1, price=100_0000, fee=30, tax=10)
        pos.update(fill1)
        assert pos.fees_scaled == 30

        fill2 = _make_fill(side=Side.SELL, qty=1, price=100_0000, fee=35, tax=15)
        pos.update(fill2)
        assert pos.fees_scaled == 65

    def test_last_update_ts_set(self) -> None:
        from hft_platform.execution.positions import Position

        pos = Position(account_id="ACC1", strategy_id="STRAT1", symbol="2330")
        fill = _make_fill(side=Side.BUY, qty=1, price=100_0000)
        pos.update(fill)
        assert pos.last_update_ts == fill.match_ts_ns


class TestDrawdown:
    """Verify portfolio drawdown tracking in PositionStore."""

    @pytest.fixture()
    def store(self, monkeypatch):
        monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
        monkeypatch.setenv("HFT_DRAWDOWN_MIN_PEAK_SCALED", "2000000")
        import importlib

        import hft_platform.execution.positions as mod

        importlib.reload(mod)
        s = mod.PositionStore()
        yield s
        importlib.reload(mod)

    def test_drawdown_zero_before_any_fills(self, store) -> None:
        assert store.get_drawdown_pct() == 0.0

    def test_drawdown_zero_after_profit(self, store) -> None:
        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=100_0000))
        assert store.get_drawdown_pct() == 0.0

        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=110_0000))
        pnl = store._total_realized_pnl_scaled
        assert pnl > 0
        assert store._peak_equity_scaled == pnl
        assert store.get_drawdown_pct() == 0.0

    def test_drawdown_after_loss(self, store) -> None:
        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=100_0000))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=120_0000))
        peak = store._peak_equity_scaled
        assert peak > 0

        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=120_0000))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=110_0000))

        current = store._total_realized_pnl_scaled
        assert current < peak
        dd = store.get_drawdown_pct()
        assert dd > 0.0
        expected_dd = (peak - current) / peak
        assert abs(dd - expected_dd) < 1e-12

    def test_peak_never_decreases(self, store) -> None:
        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=100_0000))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=110_0000))
        peak1 = store._peak_equity_scaled

        store.on_fill(_make_fill(side=Side.BUY, qty=10, price=110_0000))
        store.on_fill(_make_fill(side=Side.SELL, qty=10, price=105_0000))
        peak2 = store._peak_equity_scaled

        assert peak2 == peak1


class TestDoubleFill:
    """PositionStore has no dedup — same fill processed twice doubles quantity."""

    @pytest.fixture()
    def store(self, monkeypatch):
        monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
        import importlib

        import hft_platform.execution.positions as mod

        importlib.reload(mod)
        s = mod.PositionStore()
        yield s
        importlib.reload(mod)

    def test_duplicate_fill_doubles_position(self, store) -> None:
        fill = _make_fill(side=Side.BUY, qty=5, price=100_0000)
        store.on_fill(fill)
        store.on_fill(fill)

        key = f"{fill.account_id}:{fill.strategy_id}:{fill.symbol}"
        pos = store.positions[key]
        assert pos.net_qty == 10


class TestPositionFlip:
    """Buy 5 then Sell 10 should flip to net_qty=-5 with sell price as avg."""

    def test_flip_long_to_short(self) -> None:
        from hft_platform.execution.positions import Position

        pos = Position(account_id="ACC1", strategy_id="STRAT1", symbol="2330")

        pos.update(_make_fill(side=Side.BUY, qty=5, price=100_0000))
        assert pos.net_qty == 5

        pos.update(_make_fill(side=Side.SELL, qty=10, price=110_0000))
        assert pos.net_qty == -5
        assert pos.realized_pnl_scaled == (110_0000 - 100_0000) * 5
        assert pos.avg_price_scaled == 110_0000

    def test_flip_short_to_long(self) -> None:
        from hft_platform.execution.positions import Position

        pos = Position(account_id="ACC1", strategy_id="STRAT1", symbol="2330")

        pos.update(_make_fill(side=Side.SELL, qty=5, price=110_0000))
        assert pos.net_qty == -5

        pos.update(_make_fill(side=Side.BUY, qty=10, price=100_0000))
        assert pos.net_qty == 5
        assert pos.realized_pnl_scaled == (110_0000 - 100_0000) * 5
        assert pos.avg_price_scaled == 100_0000


class TestZeroQuantityFill:
    """Fill with qty=0 should not crash and should not change position."""

    def test_zero_qty_no_crash(self) -> None:
        from hft_platform.execution.positions import Position

        pos = Position(account_id="ACC1", strategy_id="STRAT1", symbol="2330")
        fill = _make_fill(side=Side.BUY, qty=0, price=100_0000)
        pos.update(fill)
        assert pos.net_qty == 0
        assert pos.realized_pnl_scaled == 0

    def test_zero_qty_after_existing_position(self) -> None:
        from hft_platform.execution.positions import Position

        pos = Position(account_id="ACC1", strategy_id="STRAT1", symbol="2330")
        pos.update(_make_fill(side=Side.BUY, qty=5, price=100_0000))
        assert pos.net_qty == 5

        pos.update(_make_fill(side=Side.SELL, qty=0, price=200_0000))
        assert pos.net_qty == 5
        assert pos.avg_price_scaled == 100_0000
        assert pos.realized_pnl_scaled == 0


class TestEviction:
    """PositionStore evicts flat positions when at max capacity."""

    @pytest.fixture()
    def store(self, monkeypatch):
        monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
        monkeypatch.setenv("HFT_POSITIONS_MAX_SIZE", "5")
        import importlib

        import hft_platform.execution.positions as mod

        importlib.reload(mod)
        s = mod.PositionStore()
        yield s
        monkeypatch.delenv("HFT_POSITIONS_MAX_SIZE", raising=False)
        importlib.reload(mod)

    def test_eviction_triggered_at_capacity(self, store) -> None:
        for i in range(5):
            sym = f"SYM{i}"
            store.on_fill(_make_fill(side=Side.BUY, qty=1, price=100_0000, symbol=sym))

        assert len(store.positions) == 5

        for i in range(2):
            sym = f"SYM{i}"
            store.on_fill(_make_fill(side=Side.SELL, qty=1, price=100_0000, symbol=sym))

        store.on_fill(_make_fill(side=Side.BUY, qty=1, price=100_0000, symbol="NEW1"))

        assert len(store.positions) <= 5
        key_new = "ACC1:STRAT1:NEW1"
        assert key_new in store.positions


class TestRustPythonParity:
    """When Rust tracker is available, results must match Python path."""

    @staticmethod
    def _rust_available() -> bool:
        try:
            import importlib

            mod = importlib.import_module("hft_platform.rust_core")
            return hasattr(mod, "RustPositionTracker")
        except Exception:
            return False

    @pytest.fixture()
    def stores(self, monkeypatch):
        """Return (python_store, rust_store) pair. Skip if Rust unavailable."""
        if not self._rust_available():
            pytest.skip("Rust position tracker not available")

        import importlib

        import hft_platform.execution.positions as mod

        monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
        importlib.reload(mod)
        py_store = mod.PositionStore()

        monkeypatch.setenv("HFT_RUST_POSITIONS", "1")
        importlib.reload(mod)
        rust_store = mod.PositionStore()

        yield py_store, rust_store
        importlib.reload(mod)

    def test_parity_roundtrip(self, stores) -> None:
        py_store, rust_store = stores

        fills = [
            _make_fill(side=Side.BUY, qty=10, price=500_0000),
            _make_fill(side=Side.BUY, qty=5, price=510_0000),
            _make_fill(side=Side.SELL, qty=10, price=520_0000),
            _make_fill(side=Side.SELL, qty=5, price=520_0000),
        ]

        for fill in fills:
            pd_py = py_store.on_fill(fill)
            pd_rs = rust_store.on_fill(fill)

            assert pd_py.net_qty == pd_rs.net_qty, f"net_qty mismatch after {fill.fill_id}"
            if pd_py.net_qty != 0:
                assert pd_py.avg_price == pd_rs.avg_price, f"avg_price mismatch after {fill.fill_id}"
            assert pd_py.realized_pnl == pd_rs.realized_pnl, f"realized_pnl mismatch after {fill.fill_id}"

    def test_parity_flip(self, stores) -> None:
        py_store, rust_store = stores

        fills = [
            _make_fill(side=Side.BUY, qty=5, price=100_0000),
            _make_fill(side=Side.SELL, qty=10, price=110_0000),
            _make_fill(side=Side.BUY, qty=5, price=105_0000),
        ]

        for fill in fills:
            pd_py = py_store.on_fill(fill)
            pd_rs = rust_store.on_fill(fill)

            assert pd_py.net_qty == pd_rs.net_qty
            if pd_py.net_qty != 0:
                assert pd_py.avg_price == pd_rs.avg_price
            assert pd_py.realized_pnl == pd_rs.realized_pnl
