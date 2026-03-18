"""WU-09: Parallel multi-symbol backtest runner.

Uses ProcessPoolExecutor for GIL-free parallel execution.
Feature-flagged via HFT_PARALLEL_BACKTEST (default off).
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np
import structlog

logger = structlog.get_logger("backtest.parallel")


@dataclass(frozen=True, slots=True)
class SymbolBacktestResult:
    """Result for a single symbol backtest."""

    symbol: str
    data_path: str
    equity_timestamps_ns: np.ndarray
    equity_values: np.ndarray
    fill_stats: dict
    success: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MultiSymbolBacktestResult:
    """Aggregated result from parallel multi-symbol backtest."""

    results: list[SymbolBacktestResult]
    n_symbols: int
    n_success: int
    n_failed: int


def _run_single_symbol(
    strategy_module: str,
    strategy_class: str,
    strategy_id: str,
    symbol: str,
    data_path: str,
    adapter_kwargs: dict,
) -> SymbolBacktestResult:
    """Worker function for parallel execution (must be picklable)."""
    try:
        import importlib

        mod = importlib.import_module(strategy_module)
        cls = getattr(mod, strategy_class)
        strategy = cls(strategy_id=strategy_id)

        from hft_platform.backtest.adapter import HftBacktestAdapter

        adapter = HftBacktestAdapter(
            strategy=strategy,
            asset_symbol=symbol,
            data_path=data_path,
            **adapter_kwargs,
        )
        adapter.run()

        return SymbolBacktestResult(
            symbol=symbol,
            data_path=data_path,
            equity_timestamps_ns=adapter.equity_timestamps_ns.copy(),
            equity_values=adapter.equity_values.copy(),
            fill_stats=adapter.fill_stats,
            success=True,
        )
    except Exception as e:
        return SymbolBacktestResult(
            symbol=symbol,
            data_path=data_path,
            equity_timestamps_ns=np.zeros(0, dtype=np.int64),
            equity_values=np.zeros(0, dtype=np.float64),
            fill_stats={},
            success=False,
            error=f"{type(e).__name__}: {e}",
        )


class ParallelBacktestRunner:
    """Run backtests across multiple symbols in parallel.

    Uses ProcessPoolExecutor for true parallelism (no GIL).
    Falls back to sequential execution for a single symbol.
    """

    __slots__ = (
        "_strategy_module",
        "_strategy_class",
        "_strategy_id",
        "_adapter_kwargs",
        "_max_workers",
    )

    def __init__(
        self,
        strategy_module: str,
        strategy_class: str,
        strategy_id: str,
        max_workers: int | None = None,
        **adapter_kwargs: object,
    ):
        self._strategy_module = strategy_module
        self._strategy_class = strategy_class
        self._strategy_id = strategy_id
        self._adapter_kwargs = adapter_kwargs

        if max_workers is not None:
            self._max_workers = max(1, max_workers)
        else:
            env = os.environ.get("HFT_PARALLEL_BACKTEST_WORKERS")
            self._max_workers = max(1, int(env)) if env else min(4, os.cpu_count() or 1)

    def run(
        self,
        symbols_and_data: list[tuple[str, str]],
    ) -> MultiSymbolBacktestResult:
        """Run backtests for multiple (symbol, data_path) pairs.

        Args:
            symbols_and_data: List of (symbol, data_path) tuples.

        Returns:
            MultiSymbolBacktestResult with per-symbol results.
        """
        if not symbols_and_data:
            return MultiSymbolBacktestResult(
                results=[],
                n_symbols=0,
                n_success=0,
                n_failed=0,
            )

        n = len(symbols_and_data)

        # Single symbol: run directly (no process overhead)
        if n == 1:
            symbol, data_path = symbols_and_data[0]
            result = _run_single_symbol(
                self._strategy_module,
                self._strategy_class,
                self._strategy_id,
                symbol,
                data_path,
                dict(self._adapter_kwargs),
            )
            return MultiSymbolBacktestResult(
                results=[result],
                n_symbols=1,
                n_success=1 if result.success else 0,
                n_failed=0 if result.success else 1,
            )

        # Multi-symbol: parallel execution
        workers = min(self._max_workers, n)
        logger.info("parallel_backtest_start", n_symbols=n, workers=workers)

        results: list[SymbolBacktestResult] = []
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for symbol, data_path in symbols_and_data:
                future = executor.submit(
                    _run_single_symbol,
                    self._strategy_module,
                    self._strategy_class,
                    self._strategy_id,
                    symbol,
                    data_path,
                    dict(self._adapter_kwargs),
                )
                futures[future] = symbol

            for future in as_completed(futures):
                result = future.result()
                results.append(result)

        # Sort by original order
        order = {sym: i for i, (sym, _) in enumerate(symbols_and_data)}
        results.sort(key=lambda r: order.get(r.symbol, 0))

        n_success = sum(1 for r in results if r.success)
        return MultiSymbolBacktestResult(
            results=results,
            n_symbols=n,
            n_success=n_success,
            n_failed=n - n_success,
        )
