import numpy as np

from hft_platform.backtest.runner import HftBacktestConfig, HftBacktestRunner


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
