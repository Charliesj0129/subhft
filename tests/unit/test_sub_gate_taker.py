"""Tests for taker-specific sub-gates."""
from __future__ import annotations

import numpy as np

from hft_platform.alpha._sub_gates.taker import ICEvaluationGate
from hft_platform.backtest.result import BacktestResult


def _taker_result(ic_is=0.08, ic_oos=0.05):
    return BacktestResult(
        run_id="r1",
        config_hash="h1",
        instrument="TMFD6",
        strategy_name="taker_x",
        strategy_type="taker",
        engine="hftbacktest",
        queue_model="power_prob(1.5)",
        calibration_profile_id="TMFD6_2026-04-20",
        data_source="clickhouse_streaming",
        latency_profile="p95",
        pnl_pts=200.0,
        n_fills=30,
        n_trading_days=10,
        equity_curve=np.zeros((1, 10)),
        ic_is=ic_is,
        ic_oos=ic_oos,
    )


def test_ic_gate_passes_with_good_ic():
    gate = ICEvaluationGate()
    sub = gate.evaluate(_taker_result(ic_is=0.1, ic_oos=0.06),
                         config=None,
                         thresholds={"ic_is_min": 0.03, "ic_oos_min": 0.02})
    assert sub.passed


def test_ic_gate_fails_on_oos_below_threshold():
    gate = ICEvaluationGate()
    sub = gate.evaluate(_taker_result(ic_is=0.1, ic_oos=0.01),
                         config=None,
                         thresholds={"ic_is_min": 0.03, "ic_oos_min": 0.02})
    assert not sub.passed


def test_ic_gate_fails_on_is_below_threshold():
    gate = ICEvaluationGate()
    sub = gate.evaluate(_taker_result(ic_is=0.01, ic_oos=0.1),
                         config=None,
                         thresholds={"ic_is_min": 0.03, "ic_oos_min": 0.02})
    assert not sub.passed


def test_ic_gate_fails_on_missing_ic():
    gate = ICEvaluationGate()
    sub = gate.evaluate(_taker_result(ic_is=None, ic_oos=None),
                         config=None,
                         thresholds={"ic_is_min": 0.03, "ic_oos_min": 0.02})
    assert not sub.passed


def test_ic_gate_name_and_applies_to():
    g = ICEvaluationGate()
    assert g.name == "ic_evaluation"
    assert g.applies_to == {"taker"}
