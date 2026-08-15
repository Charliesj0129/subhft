"""Research adapter for the platform-owned Gate C runtime port."""

from __future__ import annotations

from typing import Any

from hft_platform.alpha.gate_c_runtime import GateCMakerRuntime, GateCRuntime, GateCTakerRuntime


def _build_maker_runtime() -> GateCMakerRuntime:
    from research.backtest.cost_models import load_cost_profile
    from research.backtest.fill_models import QueueDepletionFill
    from research.backtest.maker_engine import ClickHouseSource, LatencyProfile, MakerEngine
    from research.backtest.result_store import ResultStore

    return GateCMakerRuntime(
        load_cost_profile=load_cost_profile,
        queue_depletion_fill=QueueDepletionFill,
        clickhouse_source=ClickHouseSource,
        latency_profile=LatencyProfile,
        maker_engine=MakerEngine,
        result_store=ResultStore,
    )


def _load_taker_cost_profile(instrument: str) -> Any:
    from research.backtest.cost_models import load_cost_profile

    return load_cost_profile(instrument)


def _build_taker_runtime() -> GateCTakerRuntime:
    from research.backtest.result_store import ResultStore
    from research.backtest.taker_engine import TakerEngine

    return GateCTakerRuntime(
        load_cost_profile=_load_taker_cost_profile,
        result_store=ResultStore,
        taker_engine=TakerEngine,
    )


def build_gate_c_runtime() -> GateCRuntime:
    """Bind research backtest implementations to the canonical Gate C port."""
    from research.backtest.hft_native_runner import HftNativeRunner, ensure_hftbt_npz
    from research.backtest.types import BacktestConfig, WalkForwardConfig
    from research.registry.scorecard import compute_scorecard

    return GateCRuntime(
        hft_native_runner=HftNativeRunner,
        ensure_hftbt_npz=ensure_hftbt_npz,
        backtest_config=BacktestConfig,
        walk_forward_config=WalkForwardConfig,
        compute_scorecard=compute_scorecard,
        load_maker_runtime=_build_maker_runtime,
        load_taker_runtime=_build_taker_runtime,
    )
