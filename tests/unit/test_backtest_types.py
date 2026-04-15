"""Tests for extended BacktestResult with provenance metadata."""
import numpy as np
import pytest

from research.backtest.types import BacktestResult


def test_backtest_result_has_provenance_fields():
    """BacktestResult must include engine_type, fill_model, instrument, etc."""
    result = BacktestResult(
        signals=np.array([0.1, 0.2]),
        equity_curve=np.array([1.0, 1.01]),
        positions=np.array([0, 1]),
        sharpe_is=1.5,
        sharpe_oos=0.8,
        ic_series=np.array([0.05]),
        ic_mean=0.05,
        ic_std=0.02,
        ic_tstat=2.5,
        ic_pvalue=0.01,
        ic_halflife=10,
        sortino=1.2,
        cvar_5pct=-0.03,
        turnover=0.5,
        max_drawdown=0.1,
        regime_metrics={"high_vol_sharpe": 1.0},
        capacity_estimate=1e6,
        run_id="test-run-001",
        config_hash="abc123",
        latency_profile={"submit_ms": 36.0},
        engine_type="maker",
        fill_model="QueueDepletion(qf=0.5)",
        cost_model="TMFD6(comm=1.3,tax=0.7)",
        instrument="TMFD6",
        data_period="2026-03-01..2026-03-31",
        data_source="clickhouse://localhost:8123/hft",
        pipeline_mode="strict",
        created_at="2026-04-15T10:00:00Z",
    )
    assert result.engine_type == "maker"
    assert result.fill_model == "QueueDepletion(qf=0.5)"
    assert result.instrument == "TMFD6"
    assert result.data_period == "2026-03-01..2026-03-31"
    assert result.pipeline_mode == "strict"


def test_backtest_result_maker_optional_fields():
    """Maker-specific fields default to None for taker results."""
    result = BacktestResult(
        signals=np.array([0.1]),
        equity_curve=np.array([1.0]),
        positions=np.array([0]),
        sharpe_is=1.0,
        sharpe_oos=0.5,
        ic_series=np.array([0.05]),
        ic_mean=0.05,
        ic_std=0.02,
        ic_tstat=2.5,
        ic_pvalue=0.01,
        ic_halflife=10,
        sortino=1.0,
        cvar_5pct=-0.02,
        turnover=0.3,
        max_drawdown=0.05,
        regime_metrics={},
        capacity_estimate=1e6,
        run_id="test-002",
        config_hash="def456",
        latency_profile={},
        engine_type="taker",
        fill_model="PowerProbQueue(3.0)",
        cost_model="TXFD6(comm=0.24,tax=0.24)",
        instrument="TXFD6",
        data_period="2026-03-01..2026-03-15",
        data_source="clickhouse://localhost:8123/hft",
        pipeline_mode="strict",
        created_at="2026-04-15T10:00:00Z",
    )
    assert result.maker_scorecard is None
    assert result.per_spread_breakdown is None
    assert result.queue_fraction is None
    assert result.daily_pnl is None


def test_backtest_result_is_frozen():
    """BacktestResult must be immutable."""
    result = BacktestResult(
        signals=np.array([0.1]),
        equity_curve=np.array([1.0]),
        positions=np.array([0]),
        sharpe_is=1.0,
        sharpe_oos=0.5,
        ic_series=np.array([0.05]),
        ic_mean=0.05,
        ic_std=0.02,
        ic_tstat=2.5,
        ic_pvalue=0.01,
        ic_halflife=10,
        sortino=1.0,
        cvar_5pct=-0.02,
        turnover=0.3,
        max_drawdown=0.05,
        regime_metrics={},
        capacity_estimate=1e6,
        run_id="test-003",
        config_hash="ghi789",
        latency_profile={},
        engine_type="taker",
        fill_model="PowerProbQueue(3.0)",
        cost_model="TXFD6(comm=0.24,tax=0.24)",
        instrument="TXFD6",
        data_period="2026-03-01..2026-03-15",
        data_source="clickhouse://localhost:8123/hft",
        pipeline_mode="strict",
        created_at="2026-04-15T10:00:00Z",
    )
    with pytest.raises(AttributeError):
        result.engine_type = "maker"  # type: ignore[misc]
