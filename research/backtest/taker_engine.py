"""TakerEngine — thin wrapper around existing hft_native_runner.

No changes to the runner itself. This adapter implements BacktestEngine
protocol and maps hft_native_runner output to unified BacktestResult.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class TakerEngine:
    """Wraps existing HftNativeRunner for taker (threshold-crossing) strategies.

    Usage in pipeline:
        engine = TakerEngine()
        result = engine.run_from_runner(runner_result, instrument=..., ...)

    Note: TakerEngine does not call HftNativeRunner directly because
    _gate_c.py already handles runner instantiation and execution.
    This wrapper only adds provenance metadata to the result.
    """

    @property
    def engine_type(self) -> str:
        return "taker"

    @property
    def fill_model_name(self) -> str:
        return "PowerProbQueue(3.0)"

    def enrich_result(
        self,
        base_result: Any,
        *,
        instrument: str,
        data_period: str,
        pipeline_mode: str,
        data_source: str = "npy",
        price_scale: float = 1.0,
    ) -> Any:
        """Add provenance metadata to an existing BacktestResult from hft_native_runner.

        Round 38: also populates ``trade_pnl`` (per-round-trip points)
        from ``base_result.positions`` + ``base_result.mid_prices`` via
        :func:`project_trade_pnl_from_position_series`.  This closes the
        Round-24 gap where ``edge_per_round_trip`` and trade-axis
        sub-gates silently fell back to ``daily_pnl`` on taker runs.

        ``price_scale`` describes the unit of ``mid_prices`` (1.0 if
        already in points; 10_000 / 1_000_000 if scaled int).  Defaults
        to 1.0 because the existing hft_native_runner emits ``mid_prices``
        in points; callers passing scaled-int arrays must say so.

        If either array is absent (e.g. the runner ran in a mode that
        skipped position recording) ``trade_pnl`` stays ``None`` — the
        sub-gate fall-back path then re-engages.

        Uses dataclasses.replace() to set new fields while preserving all existing ones.
        """
        from dataclasses import replace

        from research.backtest.trade_pnl_projector import (
            project_trade_pnl_from_position_series,
        )

        trade_pnl: list[float] | None
        positions = getattr(base_result, "positions", None)
        prices = getattr(base_result, "mid_prices", None)
        try:
            trips = project_trade_pnl_from_position_series(
                positions, prices, price_scale=price_scale
            )
        except Exception:  # noqa: BLE001 — defensive; never break enrich.
            trips = []
        trade_pnl = trips if trips else None

        return replace(
            base_result,
            engine_type="taker",
            fill_model=self.fill_model_name,
            instrument=instrument,
            data_period=data_period,
            data_source=data_source,
            pipeline_mode=pipeline_mode,
            created_at=datetime.now(timezone.utc).isoformat(),
            trade_pnl=trade_pnl,
        )
