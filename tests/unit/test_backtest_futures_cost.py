"""Verify FuturesCostConfig integration."""
from research.backtest.types import BacktestConfig, FuturesCostConfig


def test_backtest_config_has_futures_cost() -> None:
    config = BacktestConfig(data_paths=["test.npz"])
    assert config.futures_cost.use_per_contract_fees is False
    assert config.futures_cost.fee_schedule_path == "config/base/fees/futures.yaml"


def test_futures_cost_config_is_frozen() -> None:
    fc = FuturesCostConfig()
    assert fc.use_per_contract_fees is False
