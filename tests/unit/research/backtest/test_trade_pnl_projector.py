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


# --- Round 38: position-series projector + taker engine wiring -------


import numpy as np

from research.backtest.trade_pnl_projector import (
    project_trade_pnl_from_position_series,
)


class TestProjectFromPositionSeries:
    def test_empty_returns_empty(self) -> None:
        assert project_trade_pnl_from_position_series([], []) == []

    def test_none_inputs_return_empty(self) -> None:
        assert project_trade_pnl_from_position_series(None, None) == []
        assert project_trade_pnl_from_position_series(None, [1.0]) == []
        assert project_trade_pnl_from_position_series([0, 1], None) == []

    def test_length_mismatch_returns_empty(self) -> None:
        assert project_trade_pnl_from_position_series([0, 1, 1], [100.0, 101.0]) == []

    def test_length_one_returns_empty(self) -> None:
        # No transition possible from a single sample.
        assert project_trade_pnl_from_position_series([1], [100.0]) == []

    def test_no_position_change_emits_no_trips(self) -> None:
        positions = np.array([0, 0, 0, 0])
        prices = np.array([100.0, 100.5, 101.0, 99.0])
        assert project_trade_pnl_from_position_series(positions, prices) == []

    def test_single_buy_and_close_emits_one_trip(self) -> None:
        # 0 -> 1 (buy @ 100) ; 1 -> 0 (sell @ 105) -> +5 pts
        positions = np.array([0, 1, 1, 0])
        prices = np.array([99.0, 100.0, 102.0, 105.0])
        trips = project_trade_pnl_from_position_series(positions, prices)
        assert len(trips) == 1
        assert math.isclose(trips[0], 5.0, abs_tol=1e-9)

    def test_short_and_cover_emits_positive_trip(self) -> None:
        # 0 -> -1 (sell @ 100) ; -1 -> 0 (buy @ 97) -> +3 pts
        positions = np.array([0, -1, 0])
        prices = np.array([99.0, 100.0, 97.0])
        trips = project_trade_pnl_from_position_series(positions, prices)
        assert math.isclose(trips[0], 3.0, abs_tol=1e-9)

    def test_loss_trip_is_negative(self) -> None:
        positions = np.array([0, 1, 0])
        prices = np.array([100.0, 100.0, 95.0])
        trips = project_trade_pnl_from_position_series(positions, prices)
        assert trips[0] < 0
        assert math.isclose(trips[0], -5.0, abs_tol=1e-9)

    def test_multi_unit_delta_splits_into_unit_fills(self) -> None:
        # Step from 0 -> 2 = two synthetic buys at the same price; then
        # one sell drops to 1, leaving one unit unmatched in the FIFO.
        positions = np.array([0, 2, 1])
        prices = np.array([100.0, 100.0, 103.0])
        trips = project_trade_pnl_from_position_series(positions, prices)
        # 2 buys @ 100, 1 sell @ 103 -> exactly ONE matched trip @ +3.
        assert len(trips) == 1
        assert math.isclose(trips[0], 3.0, abs_tol=1e-9)

    def test_price_scale_x10000_supported(self) -> None:
        # Live tick path: prices stored x10000.
        positions = np.array([0, 1, 0])
        prices = np.array([100 * 10_000, 100 * 10_000, 105 * 10_000])
        trips = project_trade_pnl_from_position_series(
            positions, prices, price_scale=10_000.0
        )
        assert math.isclose(trips[0], 5.0, abs_tol=1e-9)

    def test_non_numeric_entries_skipped_not_raised(self) -> None:
        # Defensive: a corrupted price at one step must not abort the run.
        positions = [0, 1, 1, 0]
        prices = [100.0, 100.0, "junk", 105.0]
        trips = project_trade_pnl_from_position_series(positions, prices)
        # The buy @ idx=1 (price 100) and sell @ idx=3 (price 105) still
        # match — idx=2 was a no-op (no position change) so the corrupt
        # value never participated.
        assert math.isclose(trips[0], 5.0, abs_tol=1e-9)


class TestTakerEngineEnrichPopulatesTradePnl:
    def _base_result(self, *, positions, mid_prices) -> "object":
        # Minimal stand-in for BacktestResult — TakerEngine.enrich_result
        # uses dataclasses.replace which works on the real frozen
        # dataclass.  We construct one with only the fields we care
        # about, leaving everything else at default.
        from research.backtest.types import BacktestResult

        return BacktestResult(
            signals=np.zeros(0),
            equity_curve=np.zeros(0),
            positions=positions,
            sharpe_is=0.0,
            sharpe_oos=0.0,
            ic_series=np.zeros(0),
            ic_mean=0.0,
            ic_std=0.0,
            ic_tstat=0.0,
            ic_pvalue=0.0,
            ic_halflife=0,
            sortino=0.0,
            cvar_5pct=0.0,
            turnover=0.0,
            max_drawdown=0.0,
            regime_metrics={},
            capacity_estimate=0.0,
            run_id="r38_smoke",
            config_hash="x",
            latency_profile={},
            mid_prices=mid_prices,
        )

    def test_populates_trade_pnl_when_positions_and_prices_present(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.array([0, 1, 0, -1, 0]),
            mid_prices=np.array([100.0, 100.0, 103.0, 102.0, 99.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
        )
        # buy @100 / sell @103 -> +3 ; sell @102 / buy @99 -> +3 ; total +6
        assert out.trade_pnl is not None
        assert len(out.trade_pnl) == 2
        assert math.isclose(sum(out.trade_pnl), 6.0, abs_tol=1e-9)

    def test_leaves_trade_pnl_none_when_mid_prices_missing(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.array([0, 1, 0]),
            mid_prices=None,
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
        )
        assert out.trade_pnl is None

    def test_leaves_trade_pnl_none_when_no_position_changes(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.zeros(8),
            mid_prices=np.arange(8.0) + 100.0,
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
        )
        # No transitions -> empty list -> stored as None (sub-gate fallback).
        assert out.trade_pnl is None

    def test_total_net_pts_allocates_cost_evenly(self) -> None:
        # Two +5 gross trips, total_net = +8 -> delta = -1 per trip ->
        # each trip becomes +4.  Invariant: sum(trade_pnl) == total_net.
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.array([0, 1, 0, 1, 0]),
            mid_prices=np.array([100.0, 100.0, 105.0, 100.0, 105.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
            total_net_pts=8.0,
        )
        assert out.trade_pnl is not None
        assert len(out.trade_pnl) == 2
        assert math.isclose(sum(out.trade_pnl), 8.0, abs_tol=1e-9)
        assert math.isclose(out.trade_pnl[0], 4.0, abs_tol=1e-9)
        assert math.isclose(out.trade_pnl[1], 4.0, abs_tol=1e-9)

    def test_total_net_pts_none_keeps_gross_trips(self) -> None:
        # Round 38 back-compat: omit total_net_pts -> gross trips returned.
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.array([0, 1, 0]),
            mid_prices=np.array([100.0, 100.0, 105.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
        )
        assert out.trade_pnl is not None
        assert math.isclose(out.trade_pnl[0], 5.0, abs_tol=1e-9)

    def test_total_net_pts_can_be_negative(self) -> None:
        # Costs exceed gross PnL -> negative net total -> per-trip
        # should reflect that loss after allocation.
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.array([0, 1, 0]),
            mid_prices=np.array([100.0, 100.0, 102.0]),  # +2 gross
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
            total_net_pts=-3.0,
        )
        assert out.trade_pnl is not None
        assert math.isclose(sum(out.trade_pnl), -3.0, abs_tol=1e-9)
        assert out.trade_pnl[0] < 0

    def test_total_net_pts_zero_collapses_to_zero(self) -> None:
        # Cost exactly cancels gross -> per-trip == 0.
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.array([0, 1, 0]),
            mid_prices=np.array([100.0, 100.0, 105.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
            total_net_pts=0.0,
        )
        assert out.trade_pnl == [0.0]

    def test_total_net_pts_ignored_when_no_trips(self) -> None:
        # No transition -> still None even with a net total argument
        # (no trips to allocate over).
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.zeros(4),
            mid_prices=np.array([100.0, 100.0, 101.0, 102.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
            total_net_pts=-2.0,
        )
        assert out.trade_pnl is None

    def test_allocation_matches_maker_per_trip_pattern(self) -> None:
        # Goal §5.1 contract: per-trip allocation mirrors maker's
        # (day_net - day_gross) / n_trips formula, applied at run-level.
        # Three trips of +10, +5, -1 = gross 14; net 11 -> delta = -1.
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.array([0, 1, 0, 1, 0, -1, 0]),
            mid_prices=np.array([100.0, 100.0, 110.0, 100.0, 105.0, 100.0, 101.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
            total_net_pts=11.0,
        )
        assert out.trade_pnl is not None
        # Gross expected: +10, +5, -1.  Net per trip = gross - 1.
        assert [round(t, 6) for t in out.trade_pnl] == [9.0, 4.0, -2.0]
        assert math.isclose(sum(out.trade_pnl), 11.0, abs_tol=1e-9)

    def test_provenance_fields_still_set(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.array([0, 1, 0]),
            mid_prices=np.array([100.0, 100.0, 105.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
        )
        # Round 38 must NOT regress Round 18's provenance population.
        assert out.engine_type == "taker"
        assert out.instrument == "TXFD6"
        assert out.data_period == "2026-01-02..2026-05-13"
        assert out.pipeline_mode == "research"
        assert out.created_at  # non-empty ISO string
