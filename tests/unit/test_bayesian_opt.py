"""Tests for Bayesian optimization tool."""
from __future__ import annotations

import json
import math
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from research.tools.bayesian_opt import (
    BayesianOptConfig,
    BayesianOptResult,
    _optimization_objective,
    run_bayesian_opt,
)


# ---------------------------------------------------------------------------
# Unit tests for _optimization_objective
# ---------------------------------------------------------------------------


class TestOptimizationObjective:
    def test_sharpe_oos_mode(self) -> None:
        result = _optimization_objective(2.5, -0.05, 0.5, "sharpe_oos")
        assert result == 2.5

    def test_risk_adjusted_mode_no_penalties(self) -> None:
        result = _optimization_objective(3.0, -0.05, 0.5, "risk_adjusted")
        assert result == 3.0

    def test_risk_adjusted_mode_with_penalties(self) -> None:
        result = _optimization_objective(3.0, -0.20, 2.0, "risk_adjusted")
        dd_penalty = max(0.0, 0.20 - 0.10) * 2.0  # 0.2
        turnover_penalty = max(0.0, 2.0 - 1.0) * 0.25  # 0.25
        expected = 3.0 - dd_penalty - turnover_penalty
        assert abs(result - expected) < 1e-10

    def test_ic_first_mode(self) -> None:
        result = _optimization_objective(2.0, -0.05, 0.8, "ic_first")
        expected = 2.0 - 0.1 * 0.8
        assert abs(result - expected) < 1e-10


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestBayesianOptConfig:
    def test_default_param_space(self) -> None:
        config = BayesianOptConfig(alpha_id="test", data_paths=["/tmp/data.npy"])
        assert config.param_space == {"signal_threshold": (0.01, 0.60, False)}

    def test_custom_param_space(self) -> None:
        ps = {"threshold": (0.1, 0.5, True)}
        config = BayesianOptConfig(alpha_id="test", data_paths=["/tmp/data.npy"], param_space=ps)
        assert config.param_space == ps

    def test_default_values(self) -> None:
        config = BayesianOptConfig(alpha_id="test", data_paths=["/tmp/data.npy"])
        assert config.n_trials == 30
        assert config.n_startup_trials == 10
        assert config.is_oos_split == 0.7
        assert config.objective == "risk_adjusted"


# ---------------------------------------------------------------------------
# Result tests
# ---------------------------------------------------------------------------


class TestBayesianOptResult:
    def test_to_dict_serialization(self) -> None:
        result = BayesianOptResult(
            best_params={"signal_threshold": 0.25},
            best_objective=3.5,
            deflated_sharpe=3.2,
            n_trials=5,
            trials=[
                {"trial_number": 0, "params": {"signal_threshold": 0.1}, "objective": 2.0},
                {"trial_number": 1, "params": {"signal_threshold": 0.25}, "objective": 3.5},
            ],
            param_importance={"signal_threshold": 1.0},
        )
        d = result.to_dict()
        assert d["best_objective"] == 3.5
        assert d["deflated_sharpe"] == 3.2
        assert d["n_trials"] == 5
        assert len(d["trials"]) == 2

        # Verify JSON-serializable
        serialized = json.dumps(d)
        assert isinstance(serialized, str)

    def test_to_dict_roundtrip(self) -> None:
        result = BayesianOptResult(
            best_params={"a": 0.1, "b": 0.5},
            best_objective=1.0,
            deflated_sharpe=0.8,
            n_trials=3,
            trials=[],
            param_importance={"a": 0.7, "b": 0.3},
        )
        d = result.to_dict()
        loaded = json.loads(json.dumps(d))
        assert loaded["best_params"] == {"a": 0.1, "b": 0.5}
        assert loaded["param_importance"] == {"a": 0.7, "b": 0.3}


# ---------------------------------------------------------------------------
# Helpers for mocking
# ---------------------------------------------------------------------------


def _make_mock_backtest_result(
    sharpe_oos: float = 1.0,
    sharpe_is: float = 1.5,
    max_drawdown: float = -0.05,
    turnover: float = 0.5,
) -> MagicMock:
    """Create a mock BacktestResult with specified metrics."""
    result = MagicMock()
    result.sharpe_oos = sharpe_oos
    result.sharpe_is = sharpe_is
    result.max_drawdown = max_drawdown
    result.turnover = turnover
    result.run_id = "mock-run-001"
    result.config_hash = "abc123"
    return result


_MOCK_LATENCY = {
    "submit_ack_latency_ms": 36.0,
    "modify_ack_latency_ms": 43.0,
    "cancel_ack_latency_ms": 47.0,
    "local_decision_pipeline_latency_us": 250,
}


# ---------------------------------------------------------------------------
# Integration test with mocked runner
# ---------------------------------------------------------------------------


class TestRunBayesianOpt:
    """Test run_bayesian_opt with mocked dependencies."""

    @patch("research.tools.latency_profiles.load_latency_profile", return_value=_MOCK_LATENCY)
    @patch("research.registry.alpha_registry.AlphaRegistry.discover")
    @patch("research.backtest.hft_native_runner.ensure_hftbt_npz", return_value="/tmp/data.npz")
    @patch("research.backtest.hft_native_runner.HftNativeRunner")
    def test_finds_optimal_region(
        self,
        mock_runner_cls: MagicMock,
        mock_ensure: MagicMock,
        mock_discover: MagicMock,
        mock_load_latency: MagicMock,
    ) -> None:
        # Setup alpha registry
        mock_alpha = MagicMock()
        mock_discover.return_value = {"test_alpha": mock_alpha}

        # Runner: objective peaks at threshold=0.25
        def _make_runner(alpha: Any, config: Any) -> MagicMock:
            threshold = float(config.signal_threshold)
            sharpe = 5.0 - 40.0 * (threshold - 0.25) ** 2
            runner = MagicMock()
            runner.run.return_value = _make_mock_backtest_result(
                sharpe_oos=sharpe, sharpe_is=sharpe + 0.5, max_drawdown=-0.03, turnover=0.4,
            )
            return runner

        mock_runner_cls.side_effect = _make_runner

        config = BayesianOptConfig(
            alpha_id="test_alpha",
            data_paths=["/tmp/data.npy"],
            n_trials=5,
            n_startup_trials=3,
            seed=42,
        )

        result = run_bayesian_opt(config)

        assert result.n_trials == 5
        assert len(result.trials) == 5
        assert 0.01 <= result.best_params["signal_threshold"] <= 0.60
        assert result.best_objective > 0

    @patch("research.tools.latency_profiles.load_latency_profile", return_value=_MOCK_LATENCY)
    @patch("research.registry.alpha_registry.AlphaRegistry.discover")
    @patch("research.backtest.hft_native_runner.ensure_hftbt_npz", return_value="/tmp/data.npz")
    @patch("research.backtest.hft_native_runner.HftNativeRunner")
    def test_deflated_sharpe_computed(
        self,
        mock_runner_cls: MagicMock,
        mock_ensure: MagicMock,
        mock_discover: MagicMock,
        mock_load_latency: MagicMock,
    ) -> None:
        mock_alpha = MagicMock()
        mock_discover.return_value = {"test_alpha": mock_alpha}

        mock_runner_cls.side_effect = lambda alpha, config: MagicMock(
            run=MagicMock(
                return_value=_make_mock_backtest_result(sharpe_oos=3.0, max_drawdown=-0.05, turnover=0.5)
            )
        )

        config = BayesianOptConfig(
            alpha_id="test_alpha",
            data_paths=["/tmp/data.npy"],
            n_trials=5,
            seed=42,
        )

        result = run_bayesian_opt(config)

        assert result.deflated_sharpe < result.best_objective
        expected_penalty = math.sqrt(2.0 * math.log(5) / 100)  # fallback n_oos=100
        assert abs((result.best_objective - result.deflated_sharpe) - expected_penalty) < 1e-6

    @patch("research.tools.latency_profiles.load_latency_profile", return_value=_MOCK_LATENCY)
    @patch("research.registry.alpha_registry.AlphaRegistry.discover")
    @patch("research.backtest.hft_native_runner.ensure_hftbt_npz", return_value="/tmp/data.npz")
    @patch("research.backtest.hft_native_runner.HftNativeRunner")
    def test_trial_records_populated(
        self,
        mock_runner_cls: MagicMock,
        mock_ensure: MagicMock,
        mock_discover: MagicMock,
        mock_load_latency: MagicMock,
    ) -> None:
        mock_alpha = MagicMock()
        mock_discover.return_value = {"test_alpha": mock_alpha}

        mock_runner_cls.side_effect = lambda alpha, config: MagicMock(
            run=MagicMock(return_value=_make_mock_backtest_result(sharpe_oos=2.0))
        )

        config = BayesianOptConfig(
            alpha_id="test_alpha",
            data_paths=["/tmp/data.npy"],
            n_trials=5,
            seed=42,
        )

        result = run_bayesian_opt(config)

        assert len(result.trials) == 5
        for trial in result.trials:
            assert "trial_number" in trial
            assert "params" in trial
            assert "sharpe_oos" in trial
            assert "objective" in trial

    @patch("research.tools.latency_profiles.load_latency_profile", return_value=_MOCK_LATENCY)
    @patch("research.registry.alpha_registry.AlphaRegistry.discover")
    @patch("research.backtest.hft_native_runner.ensure_hftbt_npz", return_value="/tmp/data.npz")
    def test_alpha_not_found_raises(
        self,
        mock_ensure: MagicMock,
        mock_discover: MagicMock,
        mock_load_latency: MagicMock,
    ) -> None:
        mock_discover.return_value = {}

        config = BayesianOptConfig(
            alpha_id="nonexistent",
            data_paths=["/tmp/data.npy"],
            n_trials=5,
        )

        with pytest.raises(ValueError, match="not found"):
            run_bayesian_opt(config)
