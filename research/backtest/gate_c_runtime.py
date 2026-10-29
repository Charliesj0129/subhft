"""Research adapter for the platform-owned Gate C runtime port."""

from __future__ import annotations

from typing import Any

from hft_platform.alpha.gate_c_runtime import GateCMakerRuntime, GateCRuntime, GateCTakerRuntime


def _build_maker_runtime() -> GateCMakerRuntime:
    from research.backtest.cost_models import load_cost_profile
    from research.backtest.fill_models import QueueDepletionFill
    from research.backtest.maker_engine import ClickHouseSource, LatencyProfile, MakerEngine
    from research.backtest.rolled_series import schedule_from_source

    return GateCMakerRuntime(
        load_cost_profile=load_cost_profile,
        queue_depletion_fill=QueueDepletionFill,
        clickhouse_source=ClickHouseSource,
        latency_profile=LatencyProfile,
        maker_engine=MakerEngine,
        rolled_schedule=schedule_from_source,
    )


def _load_taker_cost_profile(instrument: str) -> Any:
    from research.backtest.cost_models import load_cost_profile

    return load_cost_profile(instrument)


def _run_taker_sim(alpha: Any, cfg: Any, hbt_paths: list[str], cost_model: Any) -> Any:
    """TakerSim over every trading day of ``hbt_paths``, with the screening runner's latency."""
    from research.backtest.hft_native_runner import _effective_broker_rtt_ms
    from research.backtest.taker_sim import TakerSim, simulate_files

    sim = TakerSim(
        alpha=alpha,
        instrument=str(getattr(cost_model, "instrument", "")),
        cost_model=cost_model,
        latency_us=int(cfg.local_decision_pipeline_latency_us + _effective_broker_rtt_ms(cfg) * 1000),
        signal_threshold=float(cfg.signal_threshold),
        max_position=int(cfg.max_position),
        queue_model=str(cfg.queue_model),
        exchange_model=str(cfg.exchange_model),
        adapter_kwargs={"latency_model": str(cfg.latency_model)},
    )
    return simulate_files(sim, list(hbt_paths))


def _build_taker_runtime() -> GateCTakerRuntime:
    from research.backtest.taker_engine import TakerEngine

    return GateCTakerRuntime(
        load_cost_profile=_load_taker_cost_profile,
        taker_engine=TakerEngine,
        run_taker_sim=_run_taker_sim,
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
