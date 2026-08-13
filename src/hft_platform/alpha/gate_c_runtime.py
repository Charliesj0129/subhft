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
    result_store: type[Any]


@dataclass(frozen=True, slots=True)
class GateCTakerRuntime:
    """Research implementations loaded only after the Gate C taker run."""

    load_cost_profile: Callable[[str], Any]
    result_store: type[Any]
    taker_engine: type[Any]


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
