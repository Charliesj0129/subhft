"""Tests for maker-specific sub-gates."""

from __future__ import annotations

import numpy as np

from hft_platform.alpha._sub_gates.maker import (
    FillQualityGate,
    FillRateValidationGate,
)
from hft_platform.backtest.result import BacktestResult


def _maker_result(pnl_per_fill=1.0, adverse_fill_pct=0.3, fill_rate_per_day=5.0):
    return BacktestResult(
        run_id="r1",
        config_hash="h1",
        instrument="TMFD6",
        strategy_name="r47",
        strategy_type="maker",
        engine="hftbacktest",
        queue_model="power_prob(1.5)",
        calibration_profile_id="TMFD6_2026-04-20",
        data_source="clickhouse_streaming",
        latency_profile="p95",
        pnl_pts=100.0,
        n_fills=50,
        n_trading_days=10,
        equity_curve=np.zeros((1, 10)),
        pnl_per_fill=pnl_per_fill,
        adverse_fill_pct=adverse_fill_pct,
        fill_rate_per_day=fill_rate_per_day,
    )


# --- FillQualityGate ---


def test_fill_quality_gate_passes():
    gate = FillQualityGate()
    sub = gate.evaluate(
        _maker_result(pnl_per_fill=2.0, adverse_fill_pct=0.3),
        config=None,
        thresholds={"pnl_per_fill_min_pts": 0, "adverse_fill_pct_max": 50},
    )
    assert sub.passed


def test_fill_quality_gate_fails_on_negative_pnl_per_fill():
    gate = FillQualityGate()
    sub = gate.evaluate(
        _maker_result(pnl_per_fill=-0.5),
        config=None,
        thresholds={"pnl_per_fill_min_pts": 0, "adverse_fill_pct_max": 50},
    )
    assert not sub.passed


def test_fill_quality_gate_fails_on_high_adverse():
    gate = FillQualityGate()
    sub = gate.evaluate(
        _maker_result(adverse_fill_pct=0.7),  # 70%
        config=None,
        thresholds={"pnl_per_fill_min_pts": 0, "adverse_fill_pct_max": 50},
    )
    assert not sub.passed


def test_fill_quality_gate_name_and_applies_to():
    g = FillQualityGate()
    assert g.name == "fill_quality"
    assert g.applies_to == {"maker"}


# --- FillRateValidationGate ---


def test_fill_rate_validation_passes_within_deviation():
    gate = FillRateValidationGate()

    class FakeProfile:
        expected_fill_rate_per_day = 5.0

    sub = gate.evaluate(
        _maker_result(fill_rate_per_day=6.0),  # 20% higher
        config=None,
        thresholds={"fill_rate_deviation_max": 0.5},
        profile=FakeProfile(),
    )
    assert sub.passed


def test_fill_rate_validation_fails_large_deviation():
    gate = FillRateValidationGate()

    class FakeProfile:
        expected_fill_rate_per_day = 5.0

    sub = gate.evaluate(
        _maker_result(fill_rate_per_day=20.0),  # 300% higher
        config=None,
        thresholds={"fill_rate_deviation_max": 0.5},
        profile=FakeProfile(),
    )
    assert not sub.passed


def test_fill_rate_validation_skips_without_profile():
    gate = FillRateValidationGate()
    sub = gate.evaluate(
        _maker_result(fill_rate_per_day=6.0),
        config=None,
        thresholds={"fill_rate_deviation_max": 0.5},
        profile=None,
    )
    assert sub.passed
    assert "skipped" in sub.details.lower()


def test_fill_rate_validation_skips_when_no_fill_rate():
    gate = FillRateValidationGate()

    class FakeProfile:
        expected_fill_rate_per_day = 5.0

    # fill_rate_per_day = None
    result = BacktestResult(
        run_id="r1",
        config_hash="h1",
        instrument="TMFD6",
        strategy_name="r47",
        strategy_type="maker",
        engine="hftbacktest",
        queue_model="power_prob(1.5)",
        calibration_profile_id="TMFD6_2026-04-20",
        data_source="clickhouse_streaming",
        latency_profile="p95",
        pnl_pts=0.0,
        n_fills=0,
        n_trading_days=10,
        equity_curve=np.zeros((1, 10)),
        fill_rate_per_day=None,
    )
    sub = gate.evaluate(result, config=None, thresholds={"fill_rate_deviation_max": 0.5}, profile=FakeProfile())
    assert sub.passed
    assert "skipped" in sub.details.lower()


def test_fill_rate_validation_name_and_applies_to():
    g = FillRateValidationGate()
    assert g.name == "fill_rate_validation"
    assert g.applies_to == {"maker"}
