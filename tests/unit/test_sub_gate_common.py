"""Tests for common sub-gates applicable to both maker and taker."""
from __future__ import annotations

import numpy as np
import pytest

from hft_platform.alpha._sub_gates.common import (
    MaxDrawdownGate,
    SharpeThresholdGate,
    WinningDayPctGate,
)
from hft_platform.backtest.result import BacktestResult


def _make_result(daily_pnl: list[float], strategy_type: str = "maker") -> BacktestResult:
    return BacktestResult(
        run_id="r1",
        config_hash="h1",
        instrument="TMFD6",
        strategy_name="test",
        strategy_type=strategy_type,  # type: ignore[arg-type]
        engine="hftbacktest",
        queue_model="power_prob(1.5)",
        calibration_profile_id="TMFD6_2026-04-20",
        data_source="clickhouse_streaming",
        latency_profile="p95",
        pnl_pts=sum(daily_pnl),
        n_fills=10,
        n_trading_days=len(daily_pnl),
        equity_curve=np.cumsum(np.array([0.0] + daily_pnl)).reshape(1, -1),
        daily_pnl=daily_pnl,
    )


# --- SharpeThresholdGate ---

def test_sharpe_gate_passes_when_above_threshold():
    gate = SharpeThresholdGate()
    # Consistent positive returns -> high Sharpe
    result = _make_result([10, 12, 11, 13, 10, 14, 11, 12, 13, 10] * 3)
    sub = gate.evaluate(result, config=None,
                         thresholds={"sharpe_is_min": 0.5})
    assert sub.passed
    assert sub.metrics["sharpe"] > 0


def test_sharpe_gate_fails_when_below_threshold():
    gate = SharpeThresholdGate()
    # Near-zero mean, high vol -> low Sharpe
    result = _make_result([1, -1, 2, -2, 1, -1, 2, -2, 1, -1] * 3)
    sub = gate.evaluate(result, config=None,
                         thresholds={"sharpe_is_min": 1.0})
    assert not sub.passed


def test_sharpe_gate_empty_daily_pnl_fails():
    gate = SharpeThresholdGate()
    result = _make_result([])
    sub = gate.evaluate(result, config=None, thresholds={"sharpe_is_min": 0.5})
    assert not sub.passed
    assert "insufficient" in sub.details.lower()


def test_sharpe_gate_uses_name_sharpe_threshold():
    assert SharpeThresholdGate().name == "sharpe_threshold"


def test_sharpe_gate_applies_to_both():
    assert SharpeThresholdGate().applies_to == {"maker", "taker"}


# --- MaxDrawdownGate ---

def test_max_drawdown_gate_passes_small_dd():
    gate = MaxDrawdownGate()
    # Equity monotonically increases -> no drawdown
    result = _make_result([10, 5, 8, 12, 7, 10])
    sub = gate.evaluate(result, config=None, thresholds={"max_drawdown_pct": 50.0})
    assert sub.passed
    assert sub.metrics["max_dd_pct"] >= 0


def test_max_drawdown_gate_fails_large_dd():
    gate = MaxDrawdownGate()
    # Rise then crash: 100 -> 200 -> 50 -> 25% of peak = 75% DD
    result = _make_result([100, 100, -75, -75])
    sub = gate.evaluate(result, config=None, thresholds={"max_drawdown_pct": 30.0})
    assert not sub.passed


def test_max_drawdown_gate_no_daily_pnl_passes_trivially():
    gate = MaxDrawdownGate()
    result = _make_result([])
    sub = gate.evaluate(result, config=None, thresholds={"max_drawdown_pct": 30.0})
    assert sub.passed
    assert sub.metrics["max_dd_pct"] == 0.0


def test_max_drawdown_gate_name():
    assert MaxDrawdownGate().name == "max_drawdown"


# --- WinningDayPctGate ---

def test_winning_day_gate_passes_at_60_pct():
    gate = WinningDayPctGate()
    # 6 wins, 4 losses = 60%
    result = _make_result([1, 1, 1, 1, 1, 1, -1, -1, -1, -1])
    sub = gate.evaluate(result, config=None, thresholds={"winning_day_pct_min": 55})
    assert sub.passed
    assert sub.metrics["winning_day_pct"] == 60.0


def test_winning_day_gate_fails_at_40_pct():
    gate = WinningDayPctGate()
    result = _make_result([1, 1, 1, 1, -1, -1, -1, -1, -1, -1])
    sub = gate.evaluate(result, config=None, thresholds={"winning_day_pct_min": 55})
    assert not sub.passed


def test_winning_day_gate_zero_pnl_not_winning():
    """Day with PnL=0 should NOT count as a win (must be strictly positive)."""
    gate = WinningDayPctGate()
    result = _make_result([0, 0, 0, 0, 1, 1])
    sub = gate.evaluate(result, config=None, thresholds={"winning_day_pct_min": 30})
    # 2 out of 6 = 33.3%
    assert sub.passed
    assert sub.metrics["winning_day_pct"] == pytest.approx(33.33, abs=0.1)


def test_winning_day_gate_name():
    assert WinningDayPctGate().name == "winning_day_pct"


def test_all_common_gates_are_sub_gate_protocol():
    from hft_platform.alpha._sub_gates.registry import SubGate
    assert isinstance(SharpeThresholdGate(), SubGate)
    assert isinstance(MaxDrawdownGate(), SubGate)
    assert isinstance(WinningDayPctGate(), SubGate)
