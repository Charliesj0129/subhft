"""Parity tests: RustPositionTracker vs Python Position dataclass.

Every test runs the same fill sequence through both backends and asserts
identical net_qty, avg_price, realized_pnl, and fees.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hft_platform.contracts.execution import FillEvent, Side


def _load_rust_tracker():
    try:
        from hft_platform.rust_core import RustPositionTracker  # type: ignore[attr-defined]
    except Exception:
        try:
            from rust_core import RustPositionTracker  # type: ignore[assignment]
        except Exception:
            return None
    return RustPositionTracker


# ---------------------------------------------------------------------------
# Python reference implementation (mirrors positions.py Position.update)
# ---------------------------------------------------------------------------
class PyPosition:
    def __init__(self):
        self.net_qty = 0
        self.avg_price_scaled = 0  # i64 fixed-point
        self.realized_pnl_scaled = 0
        self.fees_scaled = 0

    def update(self, side: int, qty: int, price_scaled: int, fee: int, tax: int):
        is_buy = side == 0
        signed_fill_qty = qty if is_buy else -qty

        self.fees_scaled += fee + tax

        current_sign = 1 if self.net_qty > 0 else (-1 if self.net_qty < 0 else 0)
        fill_sign = 1 if is_buy else -1

        closing = current_sign != 0 and fill_sign != current_sign

        if closing:
            close_qty = min(abs(self.net_qty), qty)
            if is_buy:
                pnl = (self.avg_price_scaled - price_scaled) * close_qty
            else:
                pnl = (price_scaled - self.avg_price_scaled) * close_qty
            self.realized_pnl_scaled += pnl
            self.net_qty += signed_fill_qty
            if (current_sign > 0 and self.net_qty < 0) or (
                current_sign < 0 and self.net_qty > 0
            ):
                self.avg_price_scaled = price_scaled
        else:
            if self.net_qty == 0:
                self.avg_price_scaled = price_scaled
                self.net_qty += signed_fill_qty
            else:
                total_val = (
                    self.net_qty * self.avg_price_scaled
                    + signed_fill_qty * price_scaled
                )
                self.net_qty += signed_fill_qty
                if self.net_qty != 0:
                    self.avg_price_scaled = total_val // self.net_qty


BUY = 0
SELL = 1


def _make_fill(side, qty, price, fee=0, tax=0, ts=0):
    return FillEvent(
        fill_id="F1",
        account_id="ACC",
        order_id="O1",
        strategy_id="STRAT",
        symbol="SYM",
        side=Side.BUY if side == BUY else Side.SELL,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=0,
        match_ts_ns=ts,
    )


def _run_parity(fills):
    """Run fill sequence through both Rust and Python, assert identical output."""
    TrackerCls = _load_rust_tracker()
    if TrackerCls is None:
        pytest.skip("rust_core.RustPositionTracker not available")

    rust = TrackerCls()
    py = PyPosition()
    key = "ACC:STRAT:SYM"

    for side, qty, price, fee, tax in fills:
        r_net, r_avg, r_pnl, r_fees = rust.update(
            key, side, qty, price, fee, tax, 0
        )
        py.update(side, qty, price, fee, tax)

        assert r_net == py.net_qty, f"net_qty mismatch: rust={r_net} py={py.net_qty}"
        assert r_avg == py.avg_price_scaled, f"avg mismatch: rust={r_avg} py={py.avg_price_scaled}"
        assert r_pnl == py.realized_pnl_scaled, f"pnl mismatch: rust={r_pnl} py={py.realized_pnl_scaled}"
        assert r_fees == py.fees_scaled, f"fees mismatch: rust={r_fees} py={py.fees_scaled}"


def test_open_long_close():
    _run_parity([
        (BUY, 10, 10000, 5, 0),
        (SELL, 10, 10500, 5, 0),
    ])


def test_open_short_close():
    _run_parity([
        (SELL, 5, 20000, 0, 0),
        (BUY, 5, 19000, 0, 0),
    ])


def test_increase_long_weighted_avg():
    _run_parity([
        (BUY, 10, 10000, 0, 0),
        (BUY, 10, 12000, 0, 0),
    ])


def test_partial_close():
    _run_parity([
        (BUY, 10, 10000, 0, 0),
        (SELL, 3, 10500, 0, 0),
    ])


def test_flip_long_to_short():
    _run_parity([
        (BUY, 10, 10000, 0, 0),
        (SELL, 15, 11000, 0, 0),
    ])


def test_flip_short_to_long():
    _run_parity([
        (SELL, 10, 20000, 0, 0),
        (BUY, 15, 19000, 0, 0),
    ])


def test_many_fills_round_trip():
    _run_parity([
        (BUY, 10, 10000, 10, 5),
        (BUY, 5, 10200, 8, 4),
        (SELL, 8, 10300, 12, 6),
        (SELL, 7, 10100, 10, 5),
        (BUY, 20, 10050, 15, 7),
        (SELL, 20, 10400, 20, 10),
    ])


def test_close_to_flat_then_reopen():
    _run_parity([
        (BUY, 10, 10000, 0, 0),
        (SELL, 10, 10500, 0, 0),
        (SELL, 5, 11000, 0, 0),
        (BUY, 5, 10800, 0, 0),
    ])


def test_rust_get_and_reset():
    TrackerCls = _load_rust_tracker()
    if TrackerCls is None:
        pytest.skip("rust_core.RustPositionTracker not available")

    tracker = TrackerCls()
    key = "ACC:STRAT:SYM"

    assert tracker.get(key) == (0, 0, 0, 0)

    tracker.update(key, BUY, 10, 10000, 5, 0, 100)
    net, avg, pnl, fees = tracker.get(key)
    assert net == 10
    assert avg == 10000
    assert fees == 5

    tracker.reset(key)
    assert tracker.get(key) == (0, 0, 0, 0)
    assert tracker.len() == 0


def test_multiple_keys():
    TrackerCls = _load_rust_tracker()
    if TrackerCls is None:
        pytest.skip("rust_core.RustPositionTracker not available")

    tracker = TrackerCls()

    tracker.update("A:S:X", BUY, 10, 10000, 0, 0, 0)
    tracker.update("A:S:Y", SELL, 5, 20000, 0, 0, 0)

    assert tracker.len() == 2
    nx, _, _, _ = tracker.get("A:S:X")
    ny, _, _, _ = tracker.get("A:S:Y")
    assert nx == 10
    assert ny == -5
