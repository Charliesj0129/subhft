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
        assert project_trade_pnl(None) == []  # type: ignore[arg-type]

    def test_single_buy_sell_pair_emits_one_trip(self) -> None:
        fills = [_fill("buy", 100.0), _fill("sell", 105.0)]
        trips = project_trade_pnl(fills)
        assert len(trips) == 1
        assert math.isclose(trips[0], 5.0, abs_tol=1e-9)

    def test_unmatched_buys_emit_no_trip(self) -> None:
        fills = [_fill("buy", 100.0), _fill("buy", 101.0)]
        assert project_trade_pnl(fills) == []

    def test_alternating_buys_and_sells_emit_per_trip_pnl(self) -> None:
        fills = [
            _fill("buy", 100.0),
            _fill("sell", 102.0),
            _fill("buy", 99.0),
            _fill("sell", 101.0),
        ]
        trips = project_trade_pnl(fills)
        assert [round(t, 6) for t in trips] == [2.0, 2.0]

    def test_short_then_cover_emits_correct_pnl(self) -> None:
        fills = [_fill("sell", 100.0), _fill("buy", 98.0)]
        trips = project_trade_pnl(fills)
        assert math.isclose(trips[0], 2.0, abs_tol=1e-9)

    def test_total_matches_maker_engine_fifo_helper(self) -> None:
        fills = [
            _fill("buy", 100.0),
            _fill("sell", 103.5),
            _fill("sell", 104.0),
            _fill("buy", 102.5),
            _fill("buy", 101.0),
            _fill("sell", 100.0),
        ]
        trips = project_trade_pnl(fills)
        gross, n_trips, _ = MakerEngine._compute_fifo_pnl(fills)
        assert n_trips == len(trips)
        assert math.isclose(sum(trips), gross, abs_tol=1e-9)

    def test_custom_price_scale_honoured(self) -> None:
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
        from hft_platform.alpha._sub_gates.trade_concentration import (
            TradeConcentrationGate,
        )

        fills = [
            _fill("buy", 100.0),
            _fill("sell", 110.0),
            _fill("buy", 100.0),
            _fill("sell", 102.0),
            _fill("buy", 100.0),
            _fill("sell", 101.0),
        ]
        trips = project_trade_pnl(fills)

        class _R:
            trade_pnl = trips
            daily_pnl: list = []

        out = TradeConcentrationGate().evaluate(_R(), config=None, thresholds={"top_trade_share_max_pct": 60.0})
        assert out.passed is False
        assert out.metrics["n_trades"] == 3.0


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
        assert project_trade_pnl_from_position_series([1], [100.0]) == []

    def test_no_position_change_emits_no_trips(self) -> None:
        positions = np.array([0, 0, 0, 0])
        prices = np.array([100.0, 100.5, 101.0, 99.0])
        assert project_trade_pnl_from_position_series(positions, prices) == []

    def test_single_buy_and_close_emits_one_trip(self) -> None:
        positions = np.array([0, 1, 1, 0])
        prices = np.array([99.0, 100.0, 102.0, 105.0])
        trips = project_trade_pnl_from_position_series(positions, prices)
        assert len(trips) == 1
        assert math.isclose(trips[0], 5.0, abs_tol=1e-9)

    def test_short_and_cover_emits_positive_trip(self) -> None:
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
        positions = np.array([0, 2, 1])
        prices = np.array([100.0, 100.0, 103.0])
        trips = project_trade_pnl_from_position_series(positions, prices)
        assert len(trips) == 2
        assert [round(t, 6) for t in trips] == [3.0, 3.0]

    def test_force_flat_disabled_preserves_residual(self) -> None:
        positions = np.array([0, 2, 1])
        prices = np.array([100.0, 100.0, 103.0])
        trips = project_trade_pnl_from_position_series(positions, prices, force_flat_at_end=False)
        assert len(trips) == 1
        assert math.isclose(trips[0], 3.0, abs_tol=1e-9)

    def test_price_scale_x10000_supported(self) -> None:
        positions = np.array([0, 1, 0])
        prices = np.array([100 * 10_000, 100 * 10_000, 105 * 10_000])
        trips = project_trade_pnl_from_position_series(positions, prices, price_scale=10_000.0)
        assert math.isclose(trips[0], 5.0, abs_tol=1e-9)

    def test_force_flat_short_residual_emits_buy_close(self) -> None:
        positions = np.array([0, -1, -1])
        prices = np.array([99.0, 100.0, 95.0])
        trips = project_trade_pnl_from_position_series(positions, prices)
        assert len(trips) == 1
        assert math.isclose(trips[0], 5.0, abs_tol=1e-9)

    def test_force_flat_long_residual_emits_loss_when_price_drops(self) -> None:
        positions = np.array([0, 1, 1])
        prices = np.array([100.0, 100.0, 92.0])
        trips = project_trade_pnl_from_position_series(positions, prices)
        assert len(trips) == 1
        assert math.isclose(trips[0], -8.0, abs_tol=1e-9)

    def test_force_flat_zero_end_position_is_noop(self) -> None:
        positions = np.array([0, 1, 0])
        prices = np.array([100.0, 100.0, 105.0])
        trips_flat = project_trade_pnl_from_position_series(positions, prices)
        trips_no_flat = project_trade_pnl_from_position_series(positions, prices, force_flat_at_end=False)
        assert trips_flat == trips_no_flat

    def test_non_numeric_entries_skipped_not_raised(self) -> None:
        positions = [0, 1, 1, 0]
        prices = [100.0, 100.0, "junk", 105.0]
        trips = project_trade_pnl_from_position_series(positions, prices)
        assert math.isclose(trips[0], 5.0, abs_tol=1e-9)


class TestTakerEngineEnrichPopulatesTradePnl:
    def _base_result(self, *, positions, mid_prices) -> "object":
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
        assert out.trade_pnl is None

    def test_total_net_pts_allocates_cost_evenly(self) -> None:
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
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.array([0, 1, 0]),
            mid_prices=np.array([100.0, 100.0, 102.0]),
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
        assert [round(t, 6) for t in out.trade_pnl] == [9.0, 4.0, -2.0]
        assert math.isclose(sum(out.trade_pnl), 11.0, abs_tol=1e-9)

    def test_cost_model_derives_total_net_pts(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        class _StubCost:
            def __init__(self, per_side: float) -> None:
                self.per_side = per_side
                self.n_fills_seen: int | None = None
                self.gross_seen: float | None = None

            def apply(self, gross_pnl_pts: float, n_fills: int) -> float:
                self.n_fills_seen = n_fills
                self.gross_seen = gross_pnl_pts
                return gross_pnl_pts - n_fills * self.per_side

        cost = _StubCost(per_side=0.5)
        base = self._base_result(
            positions=np.array([0, 1, 0, -1, 0]),
            mid_prices=np.array([100.0, 100.0, 103.0, 100.0, 99.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
            cost_model=cost,
        )
        assert cost.n_fills_seen == 4
        assert math.isclose(cost.gross_seen, 4.0, abs_tol=1e-9)
        assert out.trade_pnl is not None
        assert math.isclose(sum(out.trade_pnl), 2.0, abs_tol=1e-9)

    def test_explicit_total_net_pts_overrides_cost_model(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        class _StubCost:
            def apply(self, gross_pnl_pts: float, n_fills: int) -> float:
                return -999.0

        base = self._base_result(
            positions=np.array([0, 1, 0]),
            mid_prices=np.array([100.0, 100.0, 105.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
            total_net_pts=4.0,
            cost_model=_StubCost(),
        )
        assert out.trade_pnl == [4.0]

    def test_cost_model_raising_falls_back_to_gross(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        class _BrokenCost:
            def apply(self, gross_pnl_pts: float, n_fills: int) -> float:
                raise RuntimeError("simulated cost-model failure")

        base = self._base_result(
            positions=np.array([0, 1, 0]),
            mid_prices=np.array([100.0, 100.0, 105.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
            cost_model=_BrokenCost(),
        )
        assert out.trade_pnl == [5.0]

    def test_cost_model_with_no_trips_returns_none(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        class _Cost:
            def apply(self, gross_pnl_pts: float, n_fills: int) -> float:
                return gross_pnl_pts

        base = self._base_result(
            positions=np.zeros(4),
            mid_prices=np.array([100.0, 101.0, 102.0, 103.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="2026-01-02..2026-05-13",
            pipeline_mode="research",
            cost_model=_Cost(),
        )
        assert out.trade_pnl is None

    def test_force_flat_residual_charged_to_n_fills(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        class _StubCost:
            def __init__(self) -> None:
                self.n_fills_seen: int | None = None

            def apply(self, gross_pnl_pts: float, n_fills: int) -> float:
                self.n_fills_seen = n_fills
                return gross_pnl_pts - n_fills * 0.5

        cost = _StubCost()
        base = self._base_result(
            positions=np.array([0, 1, 1]),
            mid_prices=np.array([100.0, 100.0, 110.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="d",
            pipeline_mode="research",
            cost_model=cost,
        )
        assert cost.n_fills_seen == 2
        assert out.trade_pnl is not None
        assert math.isclose(sum(out.trade_pnl), 9.0, abs_tol=1e-9)

    def test_real_taifex_cost_model_round_trips(self) -> None:
        from research.backtest.cost_models import TAIFEXCost
        from research.backtest.taker_engine import TakerEngine

        cost = TAIFEXCost(
            instrument="TEST",
            commission_pts_per_side=0.4,
            tax_pts_per_side=0.2,
            point_value_nwd=200,
        )
        base = self._base_result(
            positions=np.array([0, 1, 0]),
            mid_prices=np.array([100.0, 100.0, 110.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TEST",
            data_period="d",
            pipeline_mode="research",
            cost_model=cost,
        )
        assert out.trade_pnl is not None
        assert math.isclose(out.trade_pnl[0], 8.8, abs_tol=1e-9)

    def test_residual_qty_recorded_when_positions_end_long(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.array([0, 1, 1]),
            mid_prices=np.array([100.0, 100.0, 105.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="d",
            pipeline_mode="research",
        )
        assert out.residual_qty == 1
        assert out.abs_residual_qty == 1
        assert out.mark_method == "force_flat_last_mid"

    def test_residual_qty_signed_negative_for_short(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.array([0, -1, -1]),
            mid_prices=np.array([100.0, 100.0, 95.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="d",
            pipeline_mode="research",
        )
        assert out.residual_qty == -1
        assert out.abs_residual_qty == 1
        assert out.mark_method == "force_flat_last_mid"

    def test_zero_residual_emits_no_residual_mark_method(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.array([0, 1, 0]),
            mid_prices=np.array([100.0, 100.0, 105.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="d",
            pipeline_mode="research",
        )
        assert out.residual_qty == 0
        assert out.abs_residual_qty == 0
        assert out.mark_method == "no_residual"

    def test_residual_qty_when_positions_missing_is_zero(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=None,
            mid_prices=None,
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="d",
            pipeline_mode="research",
        )
        assert out.residual_qty == 0
        assert out.abs_residual_qty == 0
        assert out.mark_method == "no_residual"

    def test_residual_qty_multi_unit_close(self) -> None:
        from research.backtest.taker_engine import TakerEngine

        base = self._base_result(
            positions=np.array([0, 3, 3]),
            mid_prices=np.array([100.0, 100.0, 110.0]),
        )
        out = TakerEngine().enrich_result(
            base,
            instrument="TXFD6",
            data_period="d",
            pipeline_mode="research",
        )
        assert out.residual_qty == 3
        assert out.abs_residual_qty == 3

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
        assert out.engine_type == "taker"
        assert out.instrument == "TXFD6"
        assert out.data_period == "2026-01-02..2026-05-13"
        assert out.pipeline_mode == "research"
        assert out.created_at
