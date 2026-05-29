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
        total_net_pts: float | None = None,
        cost_model: Any = None,
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

        Round 40: ``cost_model`` (optional) is any object satisfying
        :class:`research.backtest.cost_models.CostModel` — typically
        ``load_cost_profile(instrument)``.  When supplied AND
        ``total_net_pts`` is not provided, the engine derives the
        net total via ``cost_model.apply(gross_sum, n_fills)``
        where ``n_fills`` is the same synthetic-unit-fill count the
        per-trip projector uses (``sum(|position deltas|)``).  This
        is the maker / taker symmetry: maker reaches ``cost_model.apply``
        on each day's fills; taker reaches it on the run-level totals
        because ``hft_native_runner`` exposes only an aggregate equity
        curve.  Explicit ``total_net_pts`` still wins when both are
        passed (caller knows best).

        Round 39: ``total_net_pts`` (optional) is the run-level NET
        PnL in points — i.e. gross mid-to-mid minus fees / tax /
        slippage / spread / residual MtM, computed by the caller's
        cost model.  When provided, the cost delta is allocated evenly
        across all matched trips (mirrors ``MakerEngine``'s day-level
        ``(day_net - day_gross) / n_trips`` allocation, just at the
        run-level since ``hft_native_runner`` exposes a single
        aggregate equity curve, not per-day breakdowns).  The
        invariant ``sum(trade_pnl) == total_net_pts`` holds whenever
        at least one trip is matched.  When ``None``, trips remain
        gross mid-to-mid (Round 38 behaviour) so callers that don't
        know the net total still see correct per-trip ordering.

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
        # Round 40: if a cost_model is supplied AND no explicit
        # total_net_pts was given, derive it via the same Protocol the
        # maker engine uses: cost_model.apply(gross_sum, n_fills).
        # n_fills mirrors the synthetic-unit-fill expansion in
        # project_trade_pnl_from_position_series — sum(|deltas|).
        if trips and cost_model is not None and total_net_pts is None:
            try:
                n_fills = 0
                for i in range(1, len(positions)):
                    n_fills += abs(int(positions[i]) - int(positions[i - 1]))
                # Round 41: force-flat residual adds abs(positions[-1])
                # synthetic close fills (the projector defaults to
                # force_flat_at_end=True so the cost must be charged
                # for those fills too).
                n_fills += abs(int(positions[-1]))
                gross_sum = float(sum(trips))
                total_net_pts = float(cost_model.apply(gross_sum, n_fills))
            except Exception:  # noqa: BLE001 — defensive; never break enrich.
                total_net_pts = None
        if trips and total_net_pts is not None:
            # Round 39 cost allocation: spread the run-level (net - gross)
            # delta evenly across trips so trade-axis sub-gates see net
            # per-trip PnL rather than mid-to-mid gross.
            gross_sum = float(sum(trips))
            delta = (float(total_net_pts) - gross_sum) / len(trips)
            trips = [t + delta for t in trips]
        trade_pnl = trips if trips else None

        # Round 42: surface residual / force-flat metadata so reviewers
        # can tell whether a candidate's edge is propped up by
        # force-flat trips (goal 驗證標準 §3 + §5).  positions[-1] is
        # the open position the projector force-flats away; recording
        # it on the result lets downstream auditors / sub-gates flag
        # candidates whose realized PnL hinges on the closing mark.
        residual_qty_signed = 0
        try:
            if positions is not None and len(positions) > 0:
                residual_qty_signed = int(positions[-1])
        except (TypeError, ValueError):
            residual_qty_signed = 0
        abs_residual_qty = abs(residual_qty_signed)
        mark_method = "force_flat_last_mid" if abs_residual_qty > 0 else "no_residual"

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
            residual_qty=residual_qty_signed,
            abs_residual_qty=abs_residual_qty,
            mark_method=mark_method,
        )
