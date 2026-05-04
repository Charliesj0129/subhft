"""Unit tests for BacktestResult dataclass shape (Slice C task 11).

These tests pin the `replay_parity_report` optional field on
``hft_platform.backtest.result.BacktestResult`` so that
``_invoke_sub_gates`` can read the parity report off the dataclass
directly without relying on ``object.__setattr__`` passthrough hacks.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from hft_platform.backtest.result import BacktestResult


def _make_minimal_result(**overrides: object) -> BacktestResult:
    """Construct a BacktestResult with all required fields filled in."""
    defaults: dict[str, object] = {
        "run_id": "test-run",
        "config_hash": "deadbeef",
        "instrument": "TMFD6",
        "strategy_name": "r47_maker",
        "strategy_type": "maker",
        "engine": "hftbacktest_v2",
        "queue_model": "PowerProbQueueModel(3.0)",
        "calibration_profile_id": "test-profile",
        "data_source": "synthetic",
        "latency_profile": "sim_p95_v2026-02-26",
        "pnl_pts": 0.0,
        "n_fills": 0,
        "n_trading_days": 1,
        "equity_curve": np.zeros(1),
        "daily_pnl": [1.0],
    }
    defaults.update(overrides)
    return BacktestResult(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
def test_backtest_result_carries_replay_parity_report() -> None:
    """The replay_parity_report field is preserved by identity (no copy/coerce)."""
    # Arrange
    report = SimpleNamespace(match_pct=96.0, total_intents=100, matched=96)

    # Act
    result = _make_minimal_result(replay_parity_report=report)

    # Assert: identity preservation; the dataclass must not copy/transform.
    assert result.replay_parity_report is report


@pytest.mark.unit
def test_backtest_result_replay_parity_report_defaults_to_none() -> None:
    """Omitting replay_parity_report yields None (default value)."""
    # Arrange + Act
    result = _make_minimal_result()

    # Assert
    assert result.replay_parity_report is None
