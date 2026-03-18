"""WU-09: Tests for parallel multi-symbol backtest runner."""

from __future__ import annotations

import numpy as np
import pytest

from hft_platform.backtest.parallel import (
    MultiSymbolBacktestResult,
    ParallelBacktestRunner,
    SymbolBacktestResult,
    _run_single_symbol,
)


def test_empty_input():
    """Empty symbol list returns empty result."""
    runner = ParallelBacktestRunner(
        strategy_module="tests.unit.test_backtest_adapter",
        strategy_class="_SimpleStrategy",
        strategy_id="test",
    )
    result = runner.run([])
    assert isinstance(result, MultiSymbolBacktestResult)
    assert result.n_symbols == 0
    assert result.n_success == 0
    assert result.n_failed == 0
    assert result.results == []


def test_symbol_backtest_result_frozen():
    """SymbolBacktestResult is frozen dataclass."""
    r = SymbolBacktestResult(
        symbol="X",
        data_path="d",
        equity_timestamps_ns=np.zeros(0, dtype=np.int64),
        equity_values=np.zeros(0, dtype=np.float64),
        fill_stats={},
        success=True,
    )
    assert r.symbol == "X"
    assert r.success is True
    with pytest.raises(AttributeError):
        r.symbol = "Y"  # type: ignore[misc]


def test_multi_symbol_result_frozen():
    """MultiSymbolBacktestResult is frozen dataclass."""
    r = MultiSymbolBacktestResult(
        results=[],
        n_symbols=0,
        n_success=0,
        n_failed=0,
    )
    assert r.n_symbols == 0
    with pytest.raises(AttributeError):
        r.n_symbols = 1  # type: ignore[misc]


def test_run_single_symbol_error_handling():
    """Worker function returns error result on failure."""
    result = _run_single_symbol(
        strategy_module="nonexistent_module_xyz",
        strategy_class="NoClass",
        strategy_id="test",
        symbol="TEST",
        data_path="/nonexistent/path",
        adapter_kwargs={},
    )
    assert isinstance(result, SymbolBacktestResult)
    assert result.success is False
    assert result.error is not None
    assert "ModuleNotFoundError" in result.error


def test_max_workers_from_env(monkeypatch):
    """max_workers respects HFT_PARALLEL_BACKTEST_WORKERS env var."""
    monkeypatch.setenv("HFT_PARALLEL_BACKTEST_WORKERS", "2")
    runner = ParallelBacktestRunner(
        strategy_module="m",
        strategy_class="C",
        strategy_id="t",
    )
    assert runner._max_workers == 2


def test_max_workers_explicit():
    """Explicit max_workers overrides env."""
    runner = ParallelBacktestRunner(
        strategy_module="m",
        strategy_class="C",
        strategy_id="t",
        max_workers=3,
    )
    assert runner._max_workers == 3


def test_max_workers_minimum_one():
    """max_workers cannot be less than 1."""
    runner = ParallelBacktestRunner(
        strategy_module="m",
        strategy_class="C",
        strategy_id="t",
        max_workers=0,
    )
    assert runner._max_workers == 1


def test_slots_on_runner():
    """ParallelBacktestRunner uses __slots__."""
    runner = ParallelBacktestRunner(
        strategy_module="m",
        strategy_class="C",
        strategy_id="t",
    )
    assert hasattr(runner, "__slots__")
