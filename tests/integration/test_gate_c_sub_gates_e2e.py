"""End-to-end test for Gate C sub-gate integration.

Verifies that when Gate C runs (via its inner logic), sub-gates are invoked
and their results are attached to report.details['sub_gates_advisory'].

Since ClickHouse is not available in this test environment, we exercise the
helper function directly rather than invoking the full `run_gate_c`.
Full end-to-end validation with real CK data is covered by the research
SOP in a separate session.
"""
from __future__ import annotations

import numpy as np
import pytest

from hft_platform.alpha._gate_c import _invoke_sub_gates_advisory


def test_e2e_maker_sub_gates_in_advisory_format():
    """Maker sub-gate results use the expected dict structure for report embedding."""
    results = _invoke_sub_gates_advisory(
        strategy_type="maker",
        result_payload={
            "run_id": "e2e-run-1",
            "config_hash": "abc123",
            "instrument": "TMFD6",
            "strategy_name": "r47_maker_pivot",
            "engine": "maker_engine",
            "queue_model": "QueueDepletionFill(qf=0.5)",
            "calibration_profile_id": "uncalibrated",
            "data_source": "clickhouse_direct",
            "latency_profile": "shioaji_sim_p95",
            "pnl_pts": 432.5,
            "n_fills": 86,
            "n_trading_days": 18,
            "equity_curve": np.cumsum(np.random.randn(18) * 10),
            "pnl_per_fill": 5.03,
            "adverse_fill_pct": 0.32,
            "fill_rate_per_day": 4.78,
            "daily_pnl": [15, -5, 20, 10, -3, 25, 18, 8, -10, 22, 16, 5, 12, 28, -8, 18, 25, 10],
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

    # Structural checks
    assert isinstance(results, list)
    assert len(results) > 0

    # Each entry must be JSON-serializable dict with expected keys
    expected_keys = {"name", "passed", "metrics", "details"}
    for entry in results:
        assert isinstance(entry, dict)
        assert expected_keys.issubset(entry.keys())

    # Specifically verify maker sub-gates ran
    names = {r["name"] for r in results}
    assert "sharpe_threshold" in names
    assert "max_drawdown" in names
    assert "winning_day_pct" in names
    assert "fill_quality" in names
    assert "fill_rate_validation" in names
    # Taker gates must NOT run for maker
    assert "ic_evaluation" not in names


def test_e2e_taker_sub_gates_in_advisory_format():
    """Taker sub-gate results use the expected dict structure."""
    equity = np.cumsum(np.random.randn(30) * 5 + 1)

    results = _invoke_sub_gates_advisory(
        strategy_type="taker",
        result_payload={
            "run_id": "e2e-run-2",
            "config_hash": "def456",
            "instrument": "TMFD6",
            "strategy_name": "aplr_macd",
            "engine": "hftbacktest_v2",
            "queue_model": "PowerProbQueueModel(3.0)",
            "calibration_profile_id": "uncalibrated",
            "data_source": "hftbt_npz",
            "latency_profile": "shioaji_sim_p95",
            "pnl_pts": float(equity[-1] - equity[0]),
            "n_fills": 420,
            "n_trading_days": 30,
            "equity_curve": equity,
            "ic_is": 0.072,
            "ic_oos": 0.048,
            "daily_pnl": list(np.diff(equity)),
        },
        thresholds={
            "sharpe_is_min": 1.0,
            "max_drawdown_pct": 30.0,
            "winning_day_pct_min": 55.0,
            "ic_is_min": 0.03,
            "ic_oos_min": 0.02,
        },
    )

    names = {r["name"] for r in results}
    assert "sharpe_threshold" in names
    assert "ic_evaluation" in names
    assert "fill_quality" not in names  # maker-only

    # Find ic_evaluation and verify it passed with our good ICs
    ic_entry = next(r for r in results if r["name"] == "ic_evaluation")
    assert ic_entry["passed"] is True
    assert ic_entry["metrics"]["ic_is"] == pytest.approx(0.072)
    assert ic_entry["metrics"]["ic_oos"] == pytest.approx(0.048)


def test_e2e_sub_gates_serializable_to_json():
    """Advisory sub-gate results must be JSON-serializable for report embedding."""
    import json

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
            "pnl_pts": 100.0,
            "n_fills": 50,
            "n_trading_days": 10,
            "equity_curve": np.zeros(10),
            "pnl_per_fill": 2.0,
            "adverse_fill_pct": 0.3,
            "fill_rate_per_day": 5.0,
            "daily_pnl": [10, -5, 8, 12, -3, 15, 4, 6, 3, 10],
        },
        thresholds={
            "sharpe_is_min": 0.0,
            "max_drawdown_pct": 100.0,
            "winning_day_pct_min": 0.0,
            "pnl_per_fill_min_pts": 0,
            "adverse_fill_pct_max": 100,
        },
    )

    # Must round-trip through JSON cleanly
    payload = json.dumps(results)
    parsed = json.loads(payload)
    assert isinstance(parsed, list)
    assert len(parsed) == len(results)
