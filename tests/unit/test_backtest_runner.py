import json
import sys
from unittest.mock import MagicMock, patch

import numpy as np

from hft_platform.backtest.runner import HftBacktestConfig, HftBacktestRunner
from hft_platform.strategy.base import BaseStrategy


class _MockStrat(BaseStrategy):
    pass


def test_backtest_runner_ensure_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    cfg = HftBacktestConfig(data=[str(tmp_path / "dummy.npz")], symbols=["AAA"])
    runner = HftBacktestRunner(cfg)

    data_path = tmp_path / "data/AAA_20241215.npz"
    runner._ensure_data(str(data_path))

    assert data_path.exists()
    loaded = np.load(data_path)
    assert "data" in loaded


def test_backtest_runner_generate_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    cfg = HftBacktestConfig(data=[str(tmp_path / "dummy.npz")], symbols=["AAA"])
    runner = HftBacktestRunner(cfg)

    runner._generate_report(123.0)

    report_path = tmp_path / "reports/demo_20241215.html"
    assert report_path.exists()


def test_backtest_runner_run_uses_cfg_data_and_writes_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data_path = tmp_path / "custom_feed.npz"
    record_out = tmp_path / "reports" / "run_summary.json"

    cfg = HftBacktestConfig(
        data=[str(data_path)],
        symbols=["AAA"],
        report=False,
        record_out=str(record_out),
    )
    runner = HftBacktestRunner(cfg)

    mock_mod = MagicMock()
    mock_mod.DemoStrategy = _MockStrat
    mock_mod.__dict__ = {"DemoStrategy": _MockStrat}

    with patch.dict(sys.modules, {"hft_platform.strategies.demo": mock_mod}):
        with patch.object(runner, "_ensure_data"):
            with patch("hft_platform.backtest.adapter.HftBacktestAdapter") as MockAdapter:
                mock_inst = MockAdapter.return_value
                mock_inst.run.return_value = True
                mock_inst.equity_timestamps_ns = np.array([1, 2, 3], dtype=np.int64)
                mock_inst.equity_values = np.array([1_000_000.0, 1_000_100.0, 1_000_050.0], dtype=np.float64)

                result = runner.run()

    assert result is not None
    assert result.run_id
    assert result.config_hash
    assert result.data_path == str(data_path)
    assert result.pnl == 50.0
    assert result.equity_points == 3
    assert record_out.exists()
    payload = json.loads(record_out.read_text())
    assert payload["run_id"]
    assert payload["config_hash"]
    assert payload["data_path"] == str(data_path)
    assert payload["pnl"] == 50.0


def test_backtest_runner_strict_equity_fails_without_equity_trace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data_path = tmp_path / "custom_feed.npz"

    cfg = HftBacktestConfig(
        data=[str(data_path)],
        symbols=["AAA"],
        report=False,
        strict_equity=True,
    )
    runner = HftBacktestRunner(cfg)

    mock_mod = MagicMock()
    mock_mod.DemoStrategy = _MockStrat
    mock_mod.__dict__ = {"DemoStrategy": _MockStrat}

    with patch.dict(sys.modules, {"hft_platform.strategies.demo": mock_mod}):
        with patch.object(runner, "_ensure_data"):
            with patch("hft_platform.backtest.adapter.HftBacktestAdapter") as MockAdapter:
                mock_inst = MockAdapter.return_value
                mock_inst.run.return_value = True
                mock_inst.equity_timestamps_ns = np.array([], dtype=np.int64)
                mock_inst.equity_values = np.array([], dtype=np.float64)

                result = runner.run()

    assert result is None
