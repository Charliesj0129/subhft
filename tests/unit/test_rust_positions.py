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

    def update(self, side: int, qty: int, price_scaled: int, fee: int, tax: int, multiplier: int = 1):
        is_buy = side == 0
        signed_fill_qty = qty if is_buy else -qty

        self.fees_scaled += fee + tax

        current_sign = 1 if self.net_qty > 0 else (-1 if self.net_qty < 0 else 0)
        fill_sign = 1 if is_buy else -1

        closing = current_sign != 0 and fill_sign != current_sign

        if closing:
            close_qty = min(abs(self.net_qty), qty)
            if is_buy:
                pnl = (self.avg_price_scaled - price_scaled) * close_qty * multiplier
            else:
                pnl = (price_scaled - self.avg_price_scaled) * close_qty * multiplier
            self.realized_pnl_scaled += pnl
            self.net_qty += signed_fill_qty
            if self.net_qty == 0:
                self.avg_price_scaled = 0
            elif (current_sign > 0 and self.net_qty < 0) or (current_sign < 0 and self.net_qty > 0):
                self.avg_price_scaled = price_scaled
        else:
            if self.net_qty == 0:
                self.avg_price_scaled = price_scaled
                self.net_qty += signed_fill_qty
            else:
                total_val = self.net_qty * self.avg_price_scaled + signed_fill_qty * price_scaled
                self.net_qty += signed_fill_qty
                if self.net_qty != 0:
                    # Round-to-nearest matching production Position.update()
                    self.avg_price_scaled = (2 * total_val + self.net_qty) // (2 * self.net_qty)


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


def _run_parity(fills, multiplier=1):
    """Run fill sequence through both Rust and Python, assert identical output."""
    TrackerCls = _load_rust_tracker()
    if TrackerCls is None:
        pytest.skip("rust_core.RustPositionTracker not available")

    rust = TrackerCls()
    py = PyPosition()
    key = "ACC:STRAT:SYM"

    for side, qty, price, fee, tax in fills:
        r_net, r_avg, r_pnl, r_fees = rust.update(key, side, qty, price, fee, tax, 0, multiplier)
        py.update(side, qty, price, fee, tax, multiplier)

        assert r_net == py.net_qty, f"net_qty mismatch: rust={r_net} py={py.net_qty}"
        assert r_avg == py.avg_price_scaled, f"avg mismatch: rust={r_avg} py={py.avg_price_scaled}"
        assert r_pnl == py.realized_pnl_scaled, f"pnl mismatch: rust={r_pnl} py={py.realized_pnl_scaled}"
        assert r_fees == py.fees_scaled, f"fees mismatch: rust={r_fees} py={py.fees_scaled}"

    return py


def test_open_long_close():
    py = _run_parity(
        [
            (BUY, 10, 10000, 5, 0),
            (SELL, 10, 10500, 5, 0),
        ]
    )
    assert py.net_qty == 0
    assert py.realized_pnl_scaled == 5000


def test_open_short_close():
    py = _run_parity(
        [
            (SELL, 5, 20000, 0, 0),
            (BUY, 5, 19000, 0, 0),
        ]
    )
    assert py.net_qty == 0
    assert py.realized_pnl_scaled == 5000


def test_increase_long_weighted_avg():
    py = _run_parity(
        [
            (BUY, 10, 10000, 0, 0),
            (BUY, 10, 12000, 0, 0),
        ]
    )
    assert py.net_qty == 20
    assert py.avg_price_scaled == 11000


def test_partial_close():
    py = _run_parity(
        [
            (BUY, 10, 10000, 0, 0),
            (SELL, 3, 10500, 0, 0),
        ]
    )
    assert py.net_qty == 7
    assert py.realized_pnl_scaled == 1500


def test_flip_long_to_short():
    py = _run_parity(
        [
            (BUY, 10, 10000, 0, 0),
            (SELL, 15, 11000, 0, 0),
        ]
    )
    assert py.net_qty == -5
    assert py.avg_price_scaled == 11000


def test_flip_short_to_long():
    py = _run_parity(
        [
            (SELL, 10, 20000, 0, 0),
            (BUY, 15, 19000, 0, 0),
        ]
    )
    assert py.net_qty == 5
    assert py.avg_price_scaled == 19000


def test_many_fills_round_trip():
    py = _run_parity(
        [
            (BUY, 10, 10000, 10, 5),
            (BUY, 5, 10200, 8, 4),
            (SELL, 8, 10300, 12, 6),
            (SELL, 7, 10100, 10, 5),
            (BUY, 20, 10050, 15, 7),
            (SELL, 20, 10400, 20, 10),
        ]
    )
    assert py.net_qty == 0


def test_close_to_flat_then_reopen():
    py = _run_parity(
        [
            (BUY, 10, 10000, 0, 0),
            (SELL, 10, 10500, 0, 0),
            (SELL, 5, 11000, 0, 0),
            (BUY, 5, 10800, 0, 0),
        ]
    )
    assert py.net_qty == 0
    assert py.realized_pnl_scaled == 6000


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


def test_futures_multiplier_tmf():
    """TMF multiplier=10: PnL should be scaled by multiplier."""
    py = _run_parity(
        [
            (BUY, 3, 10000, 0, 0),
            (SELL, 3, 10100, 0, 0),
        ],
        multiplier=10,
    )
    assert py.net_qty == 0
    # PnL = (10100 - 10000) * 3 * 10 = 3000
    assert py.realized_pnl_scaled == 3000


def test_futures_multiplier_txf():
    """TXF multiplier=200: PnL should be scaled by multiplier."""
    py = _run_parity(
        [
            (SELL, 2, 200000, 0, 0),
            (BUY, 2, 199500, 0, 0),
        ],
        multiplier=200,
    )
    assert py.net_qty == 0
    # PnL = (200000 - 199500) * 2 * 200 = 200000
    assert py.realized_pnl_scaled == 200000


def test_avg_price_rounding_edge():
    """Odd fills that trigger rounding: buy 3@1001 then buy 2@1002.

    total_val = 3*1001 + 2*1002 = 3003 + 2004 = 5007
    new_net = 5
    Exact avg = 1001.4 → rounded to nearest = 1001
    Formula: (2*5007 + 5) // (2*5) = 10019 // 10 = 1001
    """
    py = _run_parity(
        [
            (BUY, 3, 1001, 0, 0),
            (BUY, 2, 1002, 0, 0),
        ]
    )
    assert py.net_qty == 5
    assert py.avg_price_scaled == 1001


def test_close_to_flat_resets_avg():
    """After closing to flat, avg_price should be 0."""
    py = _run_parity(
        [
            (BUY, 5, 10000, 0, 0),
            (SELL, 5, 10500, 0, 0),
        ]
    )
    assert py.net_qty == 0
    assert py.avg_price_scaled == 0


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
