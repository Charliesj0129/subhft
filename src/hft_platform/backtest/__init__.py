"""Backtest package — HftBacktest adapter and utilities."""

from hft_platform.backtest.adapter import HftBacktestAdapter, StrategyHbtAdapter
from hft_platform.backtest.ch_data_source import (
    BacktestDataSource,
    ChDataSource,
    DataValidationError,
)
from hft_platform.backtest.result import BacktestResult

__all__ = [
    "HftBacktestAdapter",
    "StrategyHbtAdapter",
    "BacktestDataSource",
    "ChDataSource",
    "DataValidationError",
    "BacktestResult",
]
