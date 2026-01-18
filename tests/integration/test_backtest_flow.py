import sys
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.backtest.runner import HftBacktestConfig, HftBacktestRunner
from hft_platform.strategy.base import BaseStrategy


class MockStrat(BaseStrategy):
    pass


@pytest.fixture
def dummy_data(tmp_path):
    p = tmp_path / "feed.npz"
    p.touch()
    return str(p)


@pytest.mark.asyncio
async def test_backtest_run_flow(dummy_data):
    cfg = HftBacktestConfig(data=[dummy_data], symbols=["2330"], report=True)
    runner = HftBacktestRunner(cfg)

    mock_mod = MagicMock()
    mock_mod.DemoStrategy = MockStrat
    mock_mod.__dict__ = {"DemoStrategy": MockStrat}

    # Patch module where runner expects it
    with patch.dict(sys.modules, {"hft_platform.strategies.demo": mock_mod}):
        with patch.object(runner, "_ensure_data"):
            # Patch Adapter where it is defined/imported
            # runner.py imports it inside run() method: `from hft_platform.backtest.adapter import HftBacktestAdapter`
            # So we must patch `hft_platform.backtest.adapter.HftBacktestAdapter`
            with patch("hft_platform.backtest.adapter.HftBacktestAdapter") as MockAdapter:
                mock_inst = MockAdapter.return_value
                mock_inst.run.return_value = True

                # Patch Reporter where it is imported
                # `from hft_platform.backtest.reporting import HTMLReporter`
                with patch("hft_platform.backtest.reporting.HTMLReporter") as MockReporter:
                    runner.run()

                    MockAdapter.assert_called()
                    assert isinstance(runner.strategy_instance, MockStrat)
                    MockReporter.assert_called()  # Should be instantiated
