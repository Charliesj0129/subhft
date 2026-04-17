"""Tests that Gate C invokes sub-gates and includes results in report details."""
from __future__ import annotations

import numpy as np

from hft_platform.alpha._gate_c import _invoke_sub_gates_advisory


def test_invoke_sub_gates_maker_includes_fill_quality():
    """Maker sub-gates include fill_quality (maker-specific)."""
    results = _invoke_sub_gates_advisory(
        strategy_type="maker",
        result_payload={
            "run_id": "test-run",
            "config_hash": "abc",
            "instrument": "TMFD6",
            "strategy_name": "r47",
            "engine": "test",
            "queue_model": "test",
            "calibration_profile_id": "test",
            "data_source": "test",
            "latency_profile": "p95",
            "pnl_pts": 100.0,
            "n_fills": 50,
            "n_trading_days": 10,
            "equity_curve": np.array([0.0, 10, 20, 100]),
            "pnl_per_fill": 2.0,
            "adverse_fill_pct": 0.3,
            "fill_rate_per_day": 5.0,
            "daily_pnl": [10, 5, -3, 8, 12, -2, 15, 4, 6, 3],
        },
        thresholds={
            "sharpe_is_min": 0.5,
            "max_drawdown_pct": 30.0,
            "winning_day_pct_min": 55.0,
            "pnl_per_fill_min_pts": 0,
            "adverse_fill_pct_max": 50,
            "fill_rate_deviation_max": 0.5,
        },
    )

    names = {r["name"] for r in results}
    # Maker sub-gates should include common + maker-specific
    assert "sharpe_threshold" in names
    assert "max_drawdown" in names
    assert "winning_day_pct" in names
    assert "fill_quality" in names
    assert "fill_rate_validation" in names
    # Taker-specific should NOT be in maker
    assert "ic_evaluation" not in names


def test_invoke_sub_gates_taker_includes_ic_evaluation():
    """Taker sub-gates include ic_evaluation (taker-specific)."""
    results = _invoke_sub_gates_advisory(
        strategy_type="taker",
        result_payload={
            "run_id": "test-run",
            "config_hash": "abc",
            "instrument": "TMFD6",
            "strategy_name": "taker_x",
            "engine": "test",
            "queue_model": "test",
            "calibration_profile_id": "test",
            "data_source": "test",
            "latency_profile": "p95",
            "pnl_pts": 100.0,
            "n_fills": 50,
            "n_trading_days": 10,
            "equity_curve": np.array([0.0, 10, 20, 100]),
            "ic_is": 0.08,
            "ic_oos": 0.05,
            "daily_pnl": [10, 5, -3, 8, 12, -2, 15, 4, 6, 3],
        },
        thresholds={
            "sharpe_is_min": 0.5,
            "max_drawdown_pct": 30.0,
            "winning_day_pct_min": 55.0,
            "ic_is_min": 0.03,
            "ic_oos_min": 0.02,
        },
    )

    names = {r["name"] for r in results}
    assert "sharpe_threshold" in names
    assert "ic_evaluation" in names
    # Maker-specific should NOT be in taker
    assert "fill_quality" not in names
    assert "fill_rate_validation" not in names


def test_invoke_sub_gates_defensive_on_error():
    """Broken result payload should not crash — returns error entries."""
    results = _invoke_sub_gates_advisory(
        strategy_type="maker",
        result_payload={
            # Intentionally missing most fields to force errors
            "run_id": "test",
        },
        thresholds={},
    )
    # Should return SOMETHING — either passed entries or error entries
    assert isinstance(results, list)
    # All results should be dicts with 'name' key
    for r in results:
        assert "name" in r
        assert "passed" in r  # may be None on error


def test_invoke_sub_gates_result_structure():
    """Each sub-gate result should have name, passed, metrics, details."""
    results = _invoke_sub_gates_advisory(
        strategy_type="maker",
        result_payload={
            "run_id": "r1",
            "config_hash": "h",
            "instrument": "TMFD6",
            "strategy_name": "r47",
            "engine": "test",
            "queue_model": "test",
            "calibration_profile_id": "test",
            "data_source": "test",
            "latency_profile": "p",
            "pnl_pts": 0.0,
            "n_fills": 10,
            "n_trading_days": 5,
            "equity_curve": np.zeros(5),
            "pnl_per_fill": 1.0,
            "adverse_fill_pct": 0.2,
            "fill_rate_per_day": 2.0,
            "daily_pnl": [1, 2, 3, -1, 0],
        },
        thresholds={
            "sharpe_is_min": 0.0,
            "max_drawdown_pct": 100.0,
            "winning_day_pct_min": 0.0,
            "pnl_per_fill_min_pts": 0,
            "adverse_fill_pct_max": 100,
        },
    )
    for r in results:
        assert "name" in r
        assert "passed" in r
        assert "metrics" in r
        assert "details" in r
