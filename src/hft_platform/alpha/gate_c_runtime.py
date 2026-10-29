"""Dependency port for the offline Gate C backtest runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class GateCMakerRuntime:
    """Research implementations loaded only for the Gate C maker lane."""

    load_cost_profile: Callable[[str], Any]
    queue_depletion_fill: type[Any]
    clickhouse_source: type[Any]
    latency_profile: type[Any]
    maker_engine: type[Any]
    # (instrument, ck_source) -> the rolled schedule for a ``<ROOT>_frontmonth``
    # instrument (ADR-004). None: this runtime cannot run a rolled series.
    rolled_schedule: Callable[[str, Any], Any] | None = None


@dataclass(frozen=True, slots=True)
class GateCTakerRuntime:
    """Research implementations loaded only after the Gate C taker run."""

    load_cost_profile: Callable[[str], Any]
    taker_engine: type[Any]
    # (alpha, backtest config, hftbt event paths, cost model) -> a result with dated
    # ``days``/``daily_rows()``, ``n_is_days(split)``, ``sharpe_split(split)`` and ``method``:
    # the fill-priced taker backtest Gate C's PnL checks read. None: Gate C cannot price fills.
    run_taker_sim: Callable[[Any, Any, list[str], Any], Any] | None = None


@dataclass(frozen=True, slots=True)
class GateCRuntime:
    """Common dependencies and lane-specific loaders for Gate C."""

    hft_native_runner: type[Any]
    ensure_hftbt_npz: Callable[[str], Any]
    backtest_config: type[Any]
    walk_forward_config: type[Any]
    compute_scorecard: Callable[..., Any]
    load_maker_runtime: Callable[[], GateCMakerRuntime]
    load_taker_runtime: Callable[[], GateCTakerRuntime]
