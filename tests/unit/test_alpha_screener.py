"""Unit tests for fast signal screener (Unit 1)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.alpha._validation_types import ScreenConfig
from hft_platform.alpha.screener import ScreenResult


class TestScreenConfig:
    def test_defaults(self):
        cfg = ScreenConfig(alpha_id="test_alpha", data_paths=["data.npz"])
        assert cfg.min_ic == 0.005
        assert cfg.min_sharpe_oos == -0.5
        assert cfg.signal_threshold == 0.3

    def test_custom_kill_criteria(self):
        cfg = ScreenConfig(
            alpha_id="test_alpha",
            data_paths=["data.npz"],
            min_ic=0.01,
            min_sharpe_oos=-0.3,
        )
        assert cfg.min_ic == 0.01
        assert cfg.min_sharpe_oos == -0.3


class TestScreenResult:
    def test_to_dict(self):
        result = ScreenResult(
            screen_passed=True,
            sharpe_oos=1.5,
            ic_mean=0.02,
            max_drawdown=-0.1,
            correlation_pool_max=0.3,
            runtime_seconds=12.5,
        )
        d = result.to_dict()
        assert d["screen_passed"] is True
        assert d["sharpe_oos"] == 1.5
        assert d["kill_reason"] is None

    def test_failed_result(self):
        result = ScreenResult(
            screen_passed=False,
            sharpe_oos=-1.0,
            ic_mean=0.001,
            max_drawdown=-0.5,
            correlation_pool_max=0.0,
            runtime_seconds=5.0,
            kill_reason="IC too low",
        )
        assert not result.screen_passed
        assert result.kill_reason == "IC too low"


def _make_mock_result(sharpe_oos=1.5, ic_mean=0.03):
    mock_result = MagicMock()
    mock_result.sharpe_oos = sharpe_oos
    mock_result.ic_mean = ic_mean
    mock_result.max_drawdown = -0.1
    mock_result.sharpe_is = 2.0
    mock_result.ic_std = 0.01
    mock_result.turnover = 0.5
    mock_result.signals = [0.1, 0.2]
    mock_result.regime_metrics = {}
    mock_result.capacity_estimate = 1e6
    mock_result.latency_profile = {}
    return mock_result


class TestRunScreen:
    @patch("research.registry.alpha_registry.AlphaRegistry")
    @patch("research.registry.scorecard.compute_scorecard")
    @patch("research.backtest.hft_native_runner.HftNativeRunner")
    @patch("research.backtest.hft_native_runner.ensure_hftbt_npz")
    @patch("hft_platform.alpha._validation_helpers._ensure_project_root_on_path")
    @patch("hft_platform.alpha.experiments.ExperimentTracker")
    def test_screen_pass(
        self, mock_tracker_cls, mock_ensure_path, mock_ensure_npz, mock_runner_cls, mock_scorecard, mock_registry_cls
    ):
        mock_alpha = MagicMock()
        mock_alpha.manifest.alpha_id = "test_alpha"

        mock_runner_cls.return_value.run.return_value = _make_mock_result()

        mock_sc = MagicMock()
        mock_sc.correlation_pool_max = 0.2
        mock_scorecard.return_value = mock_sc

        mock_tracker = MagicMock()
        mock_tracker.latest_signals_by_alpha.return_value = {}
        mock_tracker_cls.return_value = mock_tracker

        mock_registry = MagicMock()
        mock_registry.discover.return_value = {"test_alpha": mock_alpha}
        mock_registry_cls.return_value = mock_registry

        config = ScreenConfig(alpha_id="test_alpha", data_paths=["/tmp/data.npz"])
        from hft_platform.alpha.screener import run_screen

        result = run_screen(config)

        assert result.screen_passed is True
        assert result.sharpe_oos == 1.5
        assert result.ic_mean == 0.03
        assert result.kill_reason is None

    @patch("research.registry.alpha_registry.AlphaRegistry")
    @patch("research.registry.scorecard.compute_scorecard")
    @patch("research.backtest.hft_native_runner.HftNativeRunner")
    @patch("research.backtest.hft_native_runner.ensure_hftbt_npz")
    @patch("hft_platform.alpha._validation_helpers._ensure_project_root_on_path")
    @patch("hft_platform.alpha.experiments.ExperimentTracker")
    def test_screen_fail_low_ic(
        self, mock_tracker_cls, mock_ensure_path, mock_ensure_npz, mock_runner_cls, mock_scorecard, mock_registry_cls
    ):
        mock_alpha = MagicMock()
        mock_alpha.manifest.alpha_id = "test_alpha"

        mock_runner_cls.return_value.run.return_value = _make_mock_result(ic_mean=0.001)

        mock_sc = MagicMock()
        mock_sc.correlation_pool_max = 0.0
        mock_scorecard.return_value = mock_sc

        mock_tracker = MagicMock()
        mock_tracker.latest_signals_by_alpha.return_value = {}
        mock_tracker_cls.return_value = mock_tracker

        mock_registry = MagicMock()
        mock_registry.discover.return_value = {"test_alpha": mock_alpha}
        mock_registry_cls.return_value = mock_registry

        config = ScreenConfig(alpha_id="test_alpha", data_paths=["/tmp/data.npz"])
        from hft_platform.alpha.screener import run_screen

        result = run_screen(config)

        assert result.screen_passed is False
        assert "IC too low" in result.kill_reason

    @patch("research.registry.alpha_registry.AlphaRegistry")
    @patch("research.registry.scorecard.compute_scorecard")
    @patch("research.backtest.hft_native_runner.HftNativeRunner")
    @patch("research.backtest.hft_native_runner.ensure_hftbt_npz")
    @patch("hft_platform.alpha._validation_helpers._ensure_project_root_on_path")
    @patch("hft_platform.alpha.experiments.ExperimentTracker")
    def test_screen_fail_low_sharpe(
        self, mock_tracker_cls, mock_ensure_path, mock_ensure_npz, mock_runner_cls, mock_scorecard, mock_registry_cls
    ):
        mock_alpha = MagicMock()
        mock_alpha.manifest.alpha_id = "test_alpha"

        mock_runner_cls.return_value.run.return_value = _make_mock_result(sharpe_oos=-1.0, ic_mean=0.02)

        mock_sc = MagicMock()
        mock_sc.correlation_pool_max = 0.0
        mock_scorecard.return_value = mock_sc

        mock_tracker = MagicMock()
        mock_tracker.latest_signals_by_alpha.return_value = {}
        mock_tracker_cls.return_value = mock_tracker

        mock_registry = MagicMock()
        mock_registry.discover.return_value = {"test_alpha": mock_alpha}
        mock_registry_cls.return_value = mock_registry

        config = ScreenConfig(alpha_id="test_alpha", data_paths=["/tmp/data.npz"])
        from hft_platform.alpha.screener import run_screen

        result = run_screen(config)

        assert result.screen_passed is False
        assert "Sharpe OOS too low" in result.kill_reason
