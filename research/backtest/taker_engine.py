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
    ) -> Any:
        """Add provenance metadata to an existing BacktestResult from hft_native_runner.

        Uses dataclasses.replace() to set new fields while preserving all existing ones.
        """
        from dataclasses import replace

        return replace(
            base_result,
            engine_type="taker",
            fill_model=self.fill_model_name,
            instrument=instrument,
            data_period=data_period,
            data_source=data_source,
            pipeline_mode=pipeline_mode,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
