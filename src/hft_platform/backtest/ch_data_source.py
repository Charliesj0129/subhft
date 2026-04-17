"""ClickHouse -> hftbacktest event_dtype streaming adapter.

Reads market data directly from ClickHouse and produces numpy arrays
conforming to hftbacktest's event_dtype specification.

Eliminates the .npz intermediate file and its associated export bugs
(notably the DEPTH_EVENT accumulation bug that caused 577x PnL overestimate).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np


class DataValidationError(RuntimeError):
    """Raised when loaded market data fails post-load sanity checks."""


@runtime_checkable
class BacktestDataSource(Protocol):
    """Protocol for backtest data sources."""

    def load_day(self, instrument: str, date: str) -> np.ndarray: ...

    def load_days(self, instrument: str, dates: list[str]) -> list[np.ndarray]: ...


class ChDataSource:
    """Streams ClickHouse market data as hftbacktest-compatible numpy arrays."""

    def __init__(
        self,
        ch_host: str = "localhost",
        ch_port: int = 9000,
        price_scale: int = 1_000_000,
    ) -> None:
        self.ch_host = ch_host
        self.ch_port = ch_port
        self.price_scale = price_scale

    def load_day(self, instrument: str, date: str, max_depth_levels: int = 5) -> np.ndarray:
        raise NotImplementedError

    def load_days(self, instrument: str, dates: list[str]) -> list[np.ndarray]:
        return [self.load_day(instrument, d) for d in dates]
