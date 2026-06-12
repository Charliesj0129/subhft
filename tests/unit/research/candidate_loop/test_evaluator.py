"""eval_v1 evaluator: planted-signal IC recovery, latency re-anchor, cost proxy."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from research.backtest.q_hat_table import QHatTable
from research.candidate_loop.evaluator import (
    DayEval,
    EvaluatorConfig,
    aggregate_split,
    discretize_with_hysteresis,
    evaluate_day,
    load_evaluator_config,
    scale_window,
    shift_label,
)
from research.candidate_loop.panels import Panel
from research.candidate_loop.schema import Window
from research.candidate_loop.validator import ValidCandidate, validate_line

CONFIG_PATH = Path(__file__).resolve().parents[4] / "config" / "research" / "candidate_loop" / "evaluator_v1.yaml"


def _cfg(**overrides: object) -> EvaluatorConfig:
    base: dict = {
        "evaluator_version": "eval_v1",
        "primitive_version": "prim_v1",
        "latency_config_version": "lat_shift_v1",
        "cost_assumption_version": "taifex_v1",
        "tick_size": {"TXFD6": 1.0},
        "cost_proxy_zscore_window": "100_events",
        "hysteresis_sigma": 0.5,
        "latency_shifts_ms": (0, 1, 5, 10),
        "horizon_decay_multipliers": (0.5, 1.0, 2.0, 4.0),
        "bucket_count": 5,
        "dir_coverage_threshold": 0.95,
        "min_valid_rows_per_day": 100,
        "signal_std_epsilon": 1e-9,
    }
    base.update(overrides)
    return EvaluatorConfig(**base)


def _candidate(**overrides: object) -> ValidCandidate:
    base: dict = {
        "name": "planted_obi_probe",
        "family": "order_book_imbalance",
        "hypothesis": "L1 imbalance predicts the next-event mid move on synthetic data.",
        "features": [{"name": "imb_l1", "formula": "book_imbalance(1)"}],
        "signal_formula": "imb_l1",
        "label": "future_mid_return(horizon='1_events')",
        "horizon": "1_events",
        "expected_sign": "positive",
    }
    base.update(overrides)
    result = validate_line(json.dumps(base), seen_hashes=set())
    assert isinstance(result, ValidCandidate), getattr(result, "detail", "")
    return result


def _planted_panel(
    n: int = 2000,
    seed: int = 7,
    day: str = "2026-04-13",
    *,
    planted: bool = True,
    dir_clean: bool = True,
    spread: np.ndarray | None = None,
) -> Panel:
    """Panel where book_imbalance(1) == x/2 and next-event mid return == x/1000."""
    rng = np.random.default_rng(seed)
    x = rng.uniform(-0.9, 0.9, size=n)
    local_ts = np.arange(n, dtype=np.int64) * 1_000_000  # 1ms grid
    returns = 0.001 * (x if planted else rng.uniform(-0.9, 0.9, size=n))
    mid = np.empty(n)
    mid[0] = 100.0
    mid[1:] = 100.0 * np.cumprod(1.0 + returns[:-1])
    cols: dict[str, np.ndarray] = {
        "exch_ts": local_ts.copy(),
        "local_ts": local_ts,
        "mid": mid,
        "microprice": mid.copy(),
        "spread_ticks": spread if spread is not None else np.ones(n),
        "trade_buy_qty": np.cumsum(np.full(n, 1.0)),
        "trade_sell_qty": np.cumsum(np.full(n, 1.0)),
    }
    for lvl in range(1, 6):
        cols[f"bid_qty_{lvl}"] = (2.0 + x) if lvl == 1 else np.full(n, 1.0)
        cols[f"ask_qty_{lvl}"] = (2.0 - x) if lvl == 1 else np.full(n, 1.0)
        cols[f"bid_px_{lvl}"] = mid - 0.5 * lvl
        cols[f"ask_px_{lvl}"] = mid + 0.5 * lvl
    meta = {"day": day, "symbol": "TXFD6", "tick_size": 1.0, "dir_clean": dir_clean}
    return Panel(columns=cols, meta=meta)


class TestBuildingBlocks:
    def test_shift_label_reanchors_at_availability_time(self) -> None:
        local_ts = np.array([0, 1, 2, 3], dtype=np.int64) * 1_000_000
        label0 = np.array([10.0, 20.0, 30.0, 40.0])
        shifted = shift_label(label0, local_ts, 1_000_000)
        np.testing.assert_allclose(shifted, [20.0, 30.0, 40.0, 40.0])

    def test_shift_label_zero_delta_is_identity(self) -> None:
        local_ts = np.array([0, 1], dtype=np.int64)
        label0 = np.array([1.0, 2.0])
        assert shift_label(label0, local_ts, 0) is label0

    def test_hysteresis_flip_counting(self) -> None:
        z = np.array([0.0, 0.6, 0.3, -0.2, -0.6, 0.6])
        pos, flips = discretize_with_hysteresis(z, 0.5)
        np.testing.assert_array_equal(pos, [0, 1, 1, 1, -1, 1])
        np.testing.assert_array_equal(flips, [1, 4, 5])

    def test_hysteresis_nan_holds_position(self) -> None:
        pos, flips = discretize_with_hysteresis(np.array([np.nan, 0.7, np.nan]), 0.5)
        np.testing.assert_array_equal(pos, [0, 1, 1])
        np.testing.assert_array_equal(flips, [1])

    def test_scale_window_event_floor_is_one(self) -> None:
        assert scale_window(Window(kind="events", count=1), 0.5).count == 1
        assert scale_window(Window(kind="time", duration_ns=1_000), 2.0).duration_ns == 2_000

    def test_load_real_config(self) -> None:
        cfg = load_evaluator_config(CONFIG_PATH)
        assert cfg.evaluator_version == "eval_v1"
        assert cfg.cost_assumption_version == "taifex_v1"
        assert cfg.latency_shifts_ms == (0, 1, 5, 10)
        assert cfg.tick_size["TXFD6"] == 1.0


class TestEvaluateDay:
    def test_planted_signal_recovers_high_ic(self) -> None:
        d = evaluate_day(_candidate(), _planted_panel(), _cfg())
        assert d.skipped_reason == ""
        assert d.counts_for_stats
        assert d.ic > 0.9
        assert d.rank_ic > 0.9

    def test_noise_signal_has_low_ic(self) -> None:
        d = evaluate_day(_candidate(), _planted_panel(planted=False), _cfg())
        assert abs(d.ic) < 0.2

    def test_one_event_horizon_dies_at_one_ms_latency(self) -> None:
        # 1ms grid + 1-event horizon: re-anchoring at t+1ms consumes the whole
        # horizon, so the shifted label is uncorrelated with the signal.
        d = evaluate_day(_candidate(), _planted_panel(), _cfg())
        assert d.latency_ics[0] > 0.9
        assert abs(d.latency_ics[1]) < 0.3

    def test_trade_imbalance_candidate_skipped_on_dirty_day(self) -> None:
        cand = _candidate(
            name="tf_probe",
            family="trade_flow",
            features=[{"name": "tf_imb", "formula": "trade_imbalance('200ms')"}],
            signal_formula="tf_imb",
        )
        d = evaluate_day(cand, _planted_panel(dir_clean=False), _cfg())
        assert d.skipped_reason == "dir_dirty"
        d_clean = evaluate_day(cand, _planted_panel(dir_clean=True), _cfg())
        assert d_clean.skipped_reason == ""

    def test_small_day_does_not_count_for_stats(self) -> None:
        d = evaluate_day(_candidate(), _planted_panel(n=80), _cfg(min_valid_rows_per_day=100))
        assert d.skipped_reason == ""
        assert not d.counts_for_stats

    def test_empty_panel_skipped(self) -> None:
        panel = Panel(columns={}, meta={"day": "2026-04-13", "symbol": "TXFD6"})
        assert evaluate_day(_candidate(), panel, _cfg()).skipped_reason == "empty_panel"

    def test_regime_filter_restricts_rows_and_records_out_ic(self) -> None:
        spread = np.where(np.arange(2000) % 2 == 0, 1.0, 3.0)
        panel = _planted_panel(spread=spread)
        cand = _candidate(regime_filter="spread_ticks() <= 2")
        d = evaluate_day(cand, panel, _cfg())
        full = evaluate_day(_candidate(), panel, _cfg())
        assert d.n_valid < full.n_valid
        assert d.regime_ic_out > 0.5  # planted signal also predicts out-of-regime

    def test_cost_proxy_produces_flips_with_depth_and_ts(self) -> None:
        d = evaluate_day(_candidate(), _planted_panel(), _cfg())
        assert d.flips > 0
        assert d.flip_ts.size == d.flips
        assert d.flip_depth.size == d.flips
        assert d.gross_pts_count > 0
        assert d.median_spread_pts == pytest.approx(1.0)


class TestAggregateSplit:
    def _days(self, n_days: int = 4, planted: bool = True) -> list[DayEval]:
        cand = _candidate()
        cfg = _cfg()
        return [
            evaluate_day(cand, _planted_panel(seed=i, day=f"2026-04-{13 + i:02d}", planted=planted), cfg)
            for i in range(n_days)
        ]

    def test_planted_split_metrics(self) -> None:
        metrics = aggregate_split(self._days(), expected_sign="positive", cfg=_cfg(), cost_per_side_pts=1.5)
        assert metrics["effective_day_count"] == 4
        assert metrics["stat_day_count"] == 4
        assert metrics["ic"] > 0.9
        assert metrics["ic_tstat"] > 2.0
        assert metrics["sign_consistency"] == 1.0
        assert metrics["day_stability"] == 1.0
        assert metrics["signal_std_zero_day_fraction"] == 0.0
        assert metrics["bucket_spread_pts"] > 0.0
        assert metrics["bucket_monotonicity"] == 1.0
        assert metrics["one_day_concentration"] < 0.6
        assert metrics["latency_0ms_score"] == pytest.approx(1.0)
        assert abs(metrics["latency_1ms_score"]) < 0.3
        assert metrics["turnover_proxy"] > 0.0
        assert metrics["required_move_threshold_pts"] == pytest.approx(2.0 * 1.5 + 1.0)

    def test_wrong_expected_sign_zeroes_consistency(self) -> None:
        metrics = aggregate_split(self._days(), expected_sign="negative", cfg=_cfg(), cost_per_side_pts=1.5)
        assert metrics["sign_consistency"] == 0.0
        assert metrics["day_stability"] == 1.0  # literal positive-IC fraction

    def test_skipped_days_reduce_effective_count(self) -> None:
        days = self._days(3)
        days.append(DayEval(day="2026-04-20", symbol="TXFD6", skipped_reason="dir_dirty"))
        metrics = aggregate_split(days, expected_sign="positive", cfg=_cfg(), cost_per_side_pts=1.5)
        assert metrics["day_count"] == 4
        assert metrics["effective_day_count"] == 3

    def test_maker_fields_present_and_bounded_when_qhat_given(self) -> None:
        metrics = aggregate_split(
            self._days(),
            expected_sign="positive",
            cfg=_cfg(),
            cost_per_side_pts=1.5,
            q_hat=QHatTable(),  # empty table -> fallback p=0.5 everywhere
            q_hat_symbol="TXFD6",
        )
        assert metrics["maker_cost_assumption_version"] == "taifex_maker_qhat_v1"
        assert metrics["maker_fill_prob_mean"] == pytest.approx(0.5)
        assert metrics["maker_required_move_threshold_pts"] <= metrics["required_move_threshold_pts"]
        assert metrics["maker_required_move_threshold_pts"] >= 2.0 * 1.5
        assert metrics["maker_cost_survival_score"] >= metrics["cost_survival_score"]

    def test_no_days_fails_closed(self) -> None:
        metrics = aggregate_split([], expected_sign="positive", cfg=_cfg(), cost_per_side_pts=1.5)
        assert metrics["effective_day_count"] == 0
        assert metrics["ic"] == 0.0
        assert metrics["signal_std_zero_day_fraction"] == 1.0
        assert metrics["one_day_concentration"] == 1.0
