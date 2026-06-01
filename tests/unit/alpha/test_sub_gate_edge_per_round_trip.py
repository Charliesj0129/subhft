"""Tests for EdgePerRoundTripGate.

Encodes goal §1/§2/§5 hard bar:
- edge = net PnL per completed FIFO round-trip trade
- ``mean_net_edge_pts_per_trade >= mean_net_edge_pts_per_trade_min``
- residual MtM is already folded into ``daily_pnl[*].pnl_pts`` by the
  maker engine, so a residual-only "trade" is not a trip.
- If no trips and no fills, gate is skipped (passes) — there is no edge
  to evaluate, sample-size gates own that failure mode.
- If trips == 0 but fills > 0 (one-sided exposure with no completed
  round-trip), the gate fails — no completed-trade edge can be claimed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hft_platform.alpha._sub_gates.edge_per_round_trip import (
    EdgePerRoundTripGate,
)


@dataclass
class _FakeResult:
    daily_pnl: list[Any] = field(default_factory=list)


def _row(pnl: float, trips: int, fills: int = 0) -> dict:
    return {
        "date": "2026-01-01",
        "pnl_pts": pnl,
        "trips": trips,
        "fills": fills if fills else trips * 2,
    }


_THRESHOLDS = {"mean_net_edge_pts_per_trade_min": 10.0}


class TestEdgePerRoundTripGate:
    def test_passes_when_edge_exceeds_floor(self) -> None:
        gate = EdgePerRoundTripGate()
        # 60 trips, total net 720 pts -> 12 pts/trip
        daily = [_row(pnl=60.0, trips=5) for _ in range(12)]
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        assert out.passed is True
        assert out.metrics["mean_net_edge_pts_per_trade"] == 12.0
        assert out.metrics["n_trips"] == 60.0

    def test_fails_when_edge_below_floor(self) -> None:
        gate = EdgePerRoundTripGate()
        # 100 trips, total net 500 pts -> 5 pts/trip
        daily = [_row(pnl=50.0, trips=10) for _ in range(10)]
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        assert out.passed is False
        assert out.metrics["mean_net_edge_pts_per_trade"] == 5.0

    def test_fails_at_exactly_the_floor_strict_inequality(self) -> None:
        # Goal §5: "> 10", not ">= 10". Edge of exactly 10 should fail.
        gate = EdgePerRoundTripGate()
        daily = [_row(pnl=100.0, trips=10) for _ in range(1)]
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        assert out.passed is False  # 10.0 == floor, fails strict >
        assert out.metrics["mean_net_edge_pts_per_trade"] == 10.0

    def test_fails_when_trips_zero_but_fills_present(self) -> None:
        gate = EdgePerRoundTripGate()
        daily = [_row(pnl=100.0, trips=0, fills=5)]
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        assert out.passed is False
        assert out.metrics["n_trips"] == 0.0
        assert out.metrics["n_fills"] == 5.0
        assert "no completed" in out.details.lower()

    def test_skipped_when_no_activity(self) -> None:
        gate = EdgePerRoundTripGate()
        r = _FakeResult(daily_pnl=[_row(pnl=0.0, trips=0, fills=0)])
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        # No fills, no trips, no PnL — there is no edge to evaluate.
        # The min_sample_size gate is the right place to fail this.
        assert out.passed is True
        assert "no activity" in out.details.lower()

    def test_negative_net_pnl_fails(self) -> None:
        gate = EdgePerRoundTripGate()
        daily = [_row(pnl=-200.0, trips=10)]
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        assert out.passed is False
        assert out.metrics["mean_net_edge_pts_per_trade"] == -20.0

    def test_handles_empty_daily(self) -> None:
        gate = EdgePerRoundTripGate()
        out = gate.evaluate(_FakeResult(), config=None, thresholds=_THRESHOLDS)
        assert out.passed is True
        assert out.metrics["n_trips"] == 0.0

    def test_attribute_name(self) -> None:
        assert EdgePerRoundTripGate.name == "edge_per_round_trip"
        assert EdgePerRoundTripGate.applies_to == {"maker", "taker"}

    def test_threshold_can_be_raised(self) -> None:
        gate = EdgePerRoundTripGate()
        daily = [_row(pnl=600.0, trips=50)]
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(r, config=None, thresholds={"mean_net_edge_pts_per_trade_min": 20.0})
        # 600/50 = 12, < 20 floor
        assert out.passed is False
        assert out.metrics["mean_net_edge_pts_per_trade"] == 12.0
        assert out.metrics["threshold_pts"] == 20.0
