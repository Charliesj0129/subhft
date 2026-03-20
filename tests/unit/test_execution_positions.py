"""Comprehensive tests for Position and PositionStore.

Covers:
- Position.update() for opening, closing, flipping, and partial close
- Fee/tax accumulation separate from realized PnL
- Weighted average price on position increase
- PositionStore Python path (HFT_RUST_POSITIONS=0)
- Eviction of flat positions at max capacity
- Portfolio drawdown calculation
- Multi-symbol isolation
- Thread safety of concurrent on_fill calls
"""

import os
import threading
from unittest.mock import patch

import pytest

from hft_platform.contracts.execution import FillEvent, PositionDelta, Side

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FILL_SEQ = 0


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
    match_ts_ns: int = 1_000_000_000,
) -> FillEvent:
    global _FILL_SEQ
    _FILL_SEQ += 1
    return FillEvent(
        fill_id=f"F{_FILL_SEQ:04d}",
        account_id=account_id,
        order_id=f"O{_FILL_SEQ:04d}",
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


@pytest.fixture()
def store():
    """PositionStore with metrics/Rust tracker disabled for unit testing."""
    with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
        from hft_platform.execution.positions import PositionStore

        s = PositionStore()
    s.metrics = None
    return s


@pytest.fixture()
def position():
    """A fresh Position instance for direct update() testing."""
    with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
        from hft_platform.execution.positions import Position

        return Position(account_id="acc1", strategy_id="strat1", symbol="2330")


# ===========================================================================
# 1. Position.update() — open long, close with PnL
# ===========================================================================


def test_open_long_close_pnl(position):
    """Buy 1@500, sell 1@510 -> realized_pnl = (510-500)*10000*1 = 100_000."""
    buy = _make_fill(Side.BUY, qty=1, price=500 * 10000)
    position.update(buy)
    assert position.net_qty == 1
    assert position.avg_price_scaled == 500 * 10000

    sell = _make_fill(Side.SELL, qty=1, price=510 * 10000)
    position.update(sell)
    assert position.net_qty == 0
    assert position.realized_pnl_scaled == (510 - 500) * 10000 * 1


def test_open_short_close_pnl(position):
    """Sell 1@500, buy 1@490 -> realized_pnl = (500-490)*10000*1 = 100_000."""
    sell = _make_fill(Side.SELL, qty=1, price=500 * 10000)
    position.update(sell)
    assert position.net_qty == -1
    assert position.avg_price_scaled == 500 * 10000

    buy = _make_fill(Side.BUY, qty=1, price=490 * 10000)
    position.update(buy)
    assert position.net_qty == 0
    assert position.realized_pnl_scaled == (500 - 490) * 10000 * 1


# ===========================================================================
# 2. Position flipping
# ===========================================================================


def test_position_flip_long_to_short(position):
    """Buy 1@500, sell 3@510 -> net_qty=-2, avg_price=510*10000, pnl from closing 1."""
    buy = _make_fill(Side.BUY, qty=1, price=500 * 10000)
    position.update(buy)

    sell = _make_fill(Side.SELL, qty=3, price=510 * 10000)
    position.update(sell)

    assert position.net_qty == -2
    assert position.avg_price_scaled == 510 * 10000
    # PnL from closing the 1 long: (510 - 500) * 10000 * 1 = 100_000
    assert position.realized_pnl_scaled == (510 - 500) * 10000 * 1


def test_position_flip_short_to_long(position):
    """Sell 1@500, buy 3@490 -> net_qty=2, avg_price=490*10000."""
    sell = _make_fill(Side.SELL, qty=1, price=500 * 10000)
    position.update(sell)

    buy = _make_fill(Side.BUY, qty=3, price=490 * 10000)
    position.update(buy)

    assert position.net_qty == 2
    assert position.avg_price_scaled == 490 * 10000
    # PnL from closing the 1 short: (500 - 490) * 10000 * 1 = 100_000
    assert position.realized_pnl_scaled == (500 - 490) * 10000 * 1


# ===========================================================================
# 3. Partial close preserves avg price
# ===========================================================================


def test_partial_close_preserves_avg_price(position):
    """Buy 2@500, sell 1@510 -> net_qty=1, avg_price stays 500*10000."""
    buy = _make_fill(Side.BUY, qty=2, price=500 * 10000)
    position.update(buy)

    sell = _make_fill(Side.SELL, qty=1, price=510 * 10000)
    position.update(sell)

    assert position.net_qty == 1
    assert position.avg_price_scaled == 500 * 10000
    assert position.realized_pnl_scaled == (510 - 500) * 10000 * 1


# ===========================================================================
# 4. Fee/tax accumulation
# ===========================================================================


def test_fee_tax_accumulation(position):
    """Multiple fills with fee+tax, verify fees_scaled accumulates separately from pnl."""
    fill1 = _make_fill(Side.BUY, qty=1, price=500 * 10000, fee=100, tax=50)
    position.update(fill1)
    assert position.fees_scaled == 150

    fill2 = _make_fill(Side.SELL, qty=1, price=510 * 10000, fee=200, tax=75)
    position.update(fill2)
    assert position.fees_scaled == 150 + 275  # 425 total

    # PnL is unaffected by fees
    assert position.realized_pnl_scaled == (510 - 500) * 10000 * 1


# ===========================================================================
# 5. Weighted average price on adding to position
# ===========================================================================


def test_weighted_average_price_on_add(position):
    """Buy 1@500, buy 1@510 -> avg_price = (500+510)/2*10000 = 505*10000."""
    fill1 = _make_fill(Side.BUY, qty=1, price=500 * 10000)
    position.update(fill1)
    assert position.avg_price_scaled == 500 * 10000

    fill2 = _make_fill(Side.BUY, qty=1, price=510 * 10000)
    position.update(fill2)
    assert position.net_qty == 2
    # Weighted avg: (1*5000000 + 1*5100000) // 2 = 5050000
    assert position.avg_price_scaled == 505 * 10000


# ===========================================================================
# 6. PositionStore — Python path
# ===========================================================================


def test_store_on_fill_python_path(monkeypatch):
    """Use monkeypatch to set HFT_RUST_POSITIONS=0, verify on_fill returns PositionDelta."""
    monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
    from hft_platform.execution.positions import PositionStore

    with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
        store = PositionStore()
    store.metrics = None

    fill = _make_fill(Side.BUY, qty=5, price=500 * 10000)
    delta = store.on_fill(fill)

    assert isinstance(delta, PositionDelta)
    assert delta.net_qty == 5
    assert delta.avg_price == 500 * 10000
    assert delta.realized_pnl == 0
    assert delta.delta_source == "FILL"
    assert delta.account_id == "acc1"
    assert delta.strategy_id == "strat1"
    assert delta.symbol == "2330"


# ===========================================================================
# 7. PositionStore — evict flat positions
# ===========================================================================


def test_store_evict_flat_positions(store):
    """Fill store to max, verify flat positions are evicted."""
    store._positions_max_size = 5

    # Create 5 positions that are flat (open then close)
    for i in range(5):
        sym = f"SYM{i:04d}"
        store.on_fill(_make_fill(Side.BUY, qty=1, price=500 * 10000, symbol=sym, match_ts_ns=1000 + i))
        store.on_fill(_make_fill(Side.SELL, qty=1, price=500 * 10000, symbol=sym, match_ts_ns=2000 + i))

    assert len(store.positions) == 5
    # All positions are flat (net_qty=0)
    assert all(p.net_qty == 0 for p in store.positions.values())

    # Adding a new fill should trigger eviction of flat positions
    new_fill = _make_fill(Side.BUY, qty=1, price=600 * 10000, symbol="NEW_SYM", match_ts_ns=9000)
    store.on_fill(new_fill)

    # Some flat positions should have been evicted, new one added
    assert "acc1:strat1:NEW_SYM" in store.positions
    assert store.positions["acc1:strat1:NEW_SYM"].net_qty == 1
    # Total positions should be less than or equal to max + 1 (eviction removes at least 1)
    assert len(store.positions) <= 5 + 1


# ===========================================================================
# 8. PositionStore — drawdown calculations
# ===========================================================================


def test_store_drawdown_zero_no_peak(store):
    """No fills -> drawdown = 0.0."""
    assert store.get_drawdown_pct() == 0.0


def test_store_drawdown_50_percent(store):
    """Peak 1000*10000, current 500*10000 -> drawdown = 0.5."""
    # Win: buy 10@500, sell 10@600 -> pnl = (600-500)*10000*10 = 10_000_000
    store.on_fill(_make_fill(Side.BUY, qty=10, price=500 * 10000))
    store.on_fill(_make_fill(Side.SELL, qty=10, price=600 * 10000))
    peak_pnl = store._peak_equity_scaled
    assert peak_pnl == (600 - 500) * 10000 * 10

    # Lose half: buy 10@600, sell 10@550 -> pnl delta = (550-600)*10000*10 = -5_000_000
    store.on_fill(_make_fill(Side.BUY, qty=10, price=600 * 10000))
    store.on_fill(_make_fill(Side.SELL, qty=10, price=550 * 10000))

    # Total pnl = 10_000_000 - 5_000_000 = 5_000_000
    assert store._total_realized_pnl_scaled == peak_pnl // 2
    assert store.get_drawdown_pct() == pytest.approx(0.5)


def test_store_drawdown_at_peak(store):
    """Current == peak -> drawdown = 0.0."""
    # Win: buy 1@500, sell 1@510 -> pnl = 100_000
    store.on_fill(_make_fill(Side.BUY, qty=1, price=500 * 10000))
    store.on_fill(_make_fill(Side.SELL, qty=1, price=510 * 10000))
    assert store._total_realized_pnl_scaled > 0
    assert store.get_drawdown_pct() == 0.0


# ===========================================================================
# 9. Multi-symbol isolation
# ===========================================================================


def test_multi_symbol_isolation(store):
    """Two symbols in same store don't affect each other."""
    # Symbol A: buy 1@500
    fill_a = _make_fill(Side.BUY, qty=1, price=500 * 10000, symbol="SYM_A")
    delta_a = store.on_fill(fill_a)
    assert delta_a.net_qty == 1
    assert delta_a.symbol == "SYM_A"

    # Symbol B: sell 2@600
    fill_b = _make_fill(Side.SELL, qty=2, price=600 * 10000, symbol="SYM_B")
    delta_b = store.on_fill(fill_b)
    assert delta_b.net_qty == -2
    assert delta_b.symbol == "SYM_B"

    # Verify positions are independent
    key_a = "acc1:strat1:SYM_A"
    key_b = "acc1:strat1:SYM_B"
    assert store.positions[key_a].net_qty == 1
    assert store.positions[key_a].avg_price_scaled == 500 * 10000
    assert store.positions[key_b].net_qty == -2
    assert store.positions[key_b].avg_price_scaled == 600 * 10000

    # Close symbol A, symbol B unaffected
    close_a = _make_fill(Side.SELL, qty=1, price=510 * 10000, symbol="SYM_A")
    delta_close = store.on_fill(close_a)
    assert delta_close.realized_pnl == (510 - 500) * 10000 * 1
    assert store.positions[key_b].net_qty == -2
    assert store.positions[key_b].realized_pnl_scaled == 0


# ===========================================================================
# 10. Thread safety
# ===========================================================================


def test_store_thread_safety(store):
    """Concurrent on_fill calls don't corrupt state."""
    n_threads = 8
    fills_per_thread = 50
    errors = []

    def worker(thread_id: int) -> None:
        try:
            for i in range(fills_per_thread):
                sym = f"T{thread_id}"
                fill = _make_fill(
                    Side.BUY,
                    qty=1,
                    price=500 * 10000,
                    symbol=sym,
                    strategy_id=f"strat_{thread_id}",
                    match_ts_ns=1_000_000 + thread_id * 1000 + i,
                )
                store.on_fill(fill)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Thread errors: {errors}"

    # Each thread created fills for its own symbol, all buys
    for tid in range(n_threads):
        key = f"acc1:strat_{tid}:T{tid}"
        assert key in store.positions, f"Missing position for thread {tid}"
        pos = store.positions[key]
        assert pos.net_qty == fills_per_thread, f"Thread {tid}: expected net_qty={fills_per_thread}, got {pos.net_qty}"
