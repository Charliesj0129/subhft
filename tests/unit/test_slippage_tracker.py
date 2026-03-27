# tests/unit/test_slippage_tracker.py
"""Tests for per-fill real-time slippage tracker."""

from __future__ import annotations

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
from hft_platform.execution.slippage_tracker import SlippageTracker


def _make_fill(
    *,
    price: int = 200_000_000,
    decision_price: int = 200_000_000,
    arrival_price: int = 200_000_000,
    qty: int = 1,
    fee: int = 130_000,
    tax: int = 0,
) -> FillEvent:
    return FillEvent(
        fill_id="f1",
        account_id="acc",
        order_id="o1",
        strategy_id="strat",
        symbol="TXFD6",
        side=Side.BUY,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=0,
        match_ts_ns=0,
        decision_price=decision_price,
        arrival_price=arrival_price,
    )


class TestSlippageTracker:
    def test_track_fill_records_metric(self) -> None:
        tracker = SlippageTracker(point_value=10)
        fill = _make_fill()
        tracker.track(fill)
        assert tracker.total_tracked == 1

    def test_slippage_bps_computed(self) -> None:
        tracker = SlippageTracker(point_value=10)
        fill = _make_fill(
            decision_price=200_000_000,
            arrival_price=200_010_000,
            price=200_020_000,
        )
        tracker.track(fill)
        assert tracker.last_slippage_bps > 0

    def test_no_crash_on_zero_decision_price(self) -> None:
        tracker = SlippageTracker(point_value=10)
        fill = _make_fill(decision_price=0, arrival_price=0)
        tracker.track(fill)
        assert tracker.total_tracked == 1
