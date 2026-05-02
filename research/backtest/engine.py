"""BacktestEngine Protocol — unified interface for taker and maker backtest."""
from __future__ import annotations

from typing import Any, Protocol

from research.backtest.types import BacktestConfig, BacktestResult


class DataSource(Protocol):
    """Protocol for backtest data sources."""
    def health_check(self) -> None: ...


class BacktestEngine(Protocol):
    """Unified backtest engine interface."""
    def run(self, config: BacktestConfig, **kwargs: Any) -> BacktestResult: ...

    @property
    def engine_type(self) -> str: ...

    @property
    def fill_model_name(self) -> str: ...
