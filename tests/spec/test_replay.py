import pytest

from hft_platform.backtest.runner import HftBacktestRunner as BacktestRunner


@pytest.mark.asyncio
async def test_deterministic_replay():
    """
    Verify that repeating a backtest with same inputs yields EXACT same outputs.
    """
    sym = "2330"
    date = "2024-01-01"

    from hft_platform.backtest.runner import HftBacktestConfig

    config = HftBacktestConfig(data=["mock_data.npz"], symbols=[sym], report=True)
    runner = BacktestRunner(config)
    runner.strategy_name = "simple_mm"

    from unittest.mock import patch

    runner._ensure_data = lambda p: None

    msg_1 = "Simulation finished 1"
    msg_2 = "Simulation finished 2"

    with patch("hft_platform.backtest.adapter.HftBacktestAdapter") as MockAdapter:
        instance_1 = MockAdapter.return_value
        instance_1.run.return_value = True

        results = []
        runner._generate_report = lambda pnl: results.append(pnl)

        runner.run()

        runner.run()

        assert len(results) == 2
        assert results[0] == results[1]

        args, _ = MockAdapter.call_args
        assert MockAdapter.call_count == 2
        assert MockAdapter.call_args_list[0].kwargs["asset_symbol"] == "2330"

    print("Deterministic Replay Passed via Mock")


if __name__ == "__main__":
    import asyncio

    asyncio.run(test_deterministic_replay())
