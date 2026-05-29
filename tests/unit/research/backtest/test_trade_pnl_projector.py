"""Tests for the round-trip / FIFO trade PnL projector (Round 24)."""

from __future__ import annotations

import math

from research.backtest.maker_engine import MakerEngine
from research.backtest.trade_pnl_projector import project_trade_pnl


_SCALE = 1_000_000


def _fill(side: str, price_pts: float) -> dict:
    return {"side": side, "price": int(round(price_pts * _SCALE))}


class TestProjectTradePnl:
    def test_empty_input_returns_empty_list(self) -> None:
        assert project_trade_pnl([]) == []

    def test_none_input_returns_empty_list(self) -> None:
        # Mirrors the engine's ``fills or []`` tolerance.
        assert project_trade_pnl(None) == []  # type: ignore[arg-type]

    def test_single_buy_sell_pair_emits_one_trip(self) -> None:
        fills = [_fill("buy", 100.0), _fill("sell", 105.0)]
        trips = project_trade_pnl(fills)
        assert len(trips) == 1
        assert math.isclose(trips[0], 5.0, abs_tol=1e-9)

    def test_unmatched_buys_emit_no_trip(self) -> None:
        fills = [_fill("buy", 100.0), _fill("buy", 101.0)]
        # Both buys sit in the FIFO; no trip closes.  Residual stays.
        assert project_trade_pnl(fills) == []

    def test_alternating_buys_and_sells_emit_per_trip_pnl(self) -> None:
        # Sequence: buy 100, sell 102 -> +2; buy 99, sell 101 -> +2.
        fills = [
            _fill("buy", 100.0),
            _fill("sell", 102.0),
            _fill("buy", 99.0),
            _fill("sell", 101.0),
        ]
        trips = project_trade_pnl(fills)
        assert [round(t, 6) for t in trips] == [2.0, 2.0]

    def test_short_then_cover_emits_correct_pnl(self) -> None:
        # Sell at 100, buy at 98 — short cover yields +2.
        fills = [_fill("sell", 100.0), _fill("buy", 98.0)]
        trips = project_trade_pnl(fills)
        assert math.isclose(trips[0], 2.0, abs_tol=1e-9)

    def test_total_matches_maker_engine_fifo_helper(self) -> None:
        # Invariant: sum(project_trade_pnl(fills)) == MakerEngine._compute_fifo_pnl(fills)[0]
        # for the same input.  This is the contract that lets engines
        # populate ``trade_pnl`` without changing aggregate PnL.
        fills = [
            _fill("buy", 100.0),
            _fill("sell", 103.5),
            _fill("sell", 104.0),
            _fill("buy", 102.5),
            _fill("buy", 101.0),
            _fill("sell", 100.0),  # losing trip
        ]
        trips = project_trade_pnl(fills)
        gross, n_trips, _ = MakerEngine._compute_fifo_pnl(fills)
        assert n_trips == len(trips)
        assert math.isclose(sum(trips), gross, abs_tol=1e-9)

    def test_custom_price_scale_honoured(self) -> None:
        # Live tick scale (x10_000) — same logical 5-point edge.
        fills = [
            {"side": "buy", "price": 100 * 10_000},
            {"side": "sell", "price": 105 * 10_000},
        ]
        trips = project_trade_pnl(fills, price_scale=10_000)
        assert math.isclose(trips[0], 5.0, abs_tol=1e-9)

    def test_loss_trip_carries_negative_sign(self) -> None:
        fills = [_fill("buy", 100.0), _fill("sell", 95.0)]
        trips = project_trade_pnl(fills)
        assert trips[0] < 0
        assert math.isclose(trips[0], -5.0, abs_tol=1e-9)

    def test_output_feeds_trade_concentration_gate_directly(self) -> None:
        # Smoke: the projector's output shape (flat list[float]) is what
        # TradeConcentrationGate consumes via _to_float_list.  Anchors the
        # downstream contract so a future shape change breaks this test
        # before reaching the gate.
        from hft_platform.alpha._sub_gates.trade_concentration import (
            TradeConcentrationGate,
        )

        fills = [
            _fill("buy", 100.0),
            _fill("sell", 110.0),  # +10
            _fill("buy", 100.0),
            _fill("sell", 102.0),  # +2
            _fill("buy", 100.0),
            _fill("sell", 101.0),  # +1
        ]
        trips = project_trade_pnl(fills)

        class _R:
            trade_pnl = trips
            daily_pnl: list = []

        out = TradeConcentrationGate().evaluate(
            _R(), config=None, thresholds={"top_trade_share_max_pct": 60.0}
        )
        # top = 10 / 13 ≈ 77 % > 60 % threshold -> fails
        assert out.passed is False
        assert out.metrics["n_trades"] == 3.0
