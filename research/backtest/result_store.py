"""ResultStore — sole official write path for backtest results."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from research.backtest.types import BacktestResult


class ResultStore:
    """Persist and query backtest results as JSON + npy."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self._base = Path(base_dir) if base_dir else Path("research/experiments/runs")

    def save(self, result: BacktestResult, alpha_id: str) -> Path:
        run_dir = self._base / result.run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        report: dict[str, Any] = {
            "alpha_id": alpha_id,
            "run_id": result.run_id,
            "engine_type": result.engine_type,
            "fill_model": result.fill_model,
            "cost_model": result.cost_model,
            "instrument": result.instrument,
            "data_period": result.data_period,
            "data_source": result.data_source,
            "config_hash": result.config_hash,
            "pipeline_mode": result.pipeline_mode,
            "created_at": result.created_at,
            "sharpe_is": result.sharpe_is,
            "sharpe_oos": result.sharpe_oos,
            "ic_mean": result.ic_mean,
            "ic_std": result.ic_std,
            "ic_tstat": result.ic_tstat,
            "ic_pvalue": result.ic_pvalue,
            "ic_halflife": result.ic_halflife,
            "sortino": result.sortino,
            "cvar_5pct": result.cvar_5pct,
            "turnover": result.turnover,
            "max_drawdown": result.max_drawdown,
            "regime_metrics": result.regime_metrics,
            "capacity_estimate": result.capacity_estimate,
            "queue_fraction": result.queue_fraction,
            "maker_scorecard": result.maker_scorecard,
            "per_spread_breakdown": result.per_spread_breakdown,
            "daily_pnl": result.daily_pnl,
        }
        (run_dir / "backtest_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str)
        )
        config_snapshot: dict[str, Any] = {
            "latency_profile": result.latency_profile,
            "config_hash": result.config_hash,
        }
        (run_dir / "config.json").write_text(
            json.dumps(config_snapshot, indent=2, sort_keys=True)
        )
        np.save(run_dir / "equity_curve.npy", result.equity_curve)
        return run_dir

    def load(self, run_id: str) -> BacktestResult:
        run_dir = self._base / run_id
        report = json.loads((run_dir / "backtest_report.json").read_text())
        config_data = json.loads((run_dir / "config.json").read_text())
        equity = np.load(run_dir / "equity_curve.npy")

        return BacktestResult(
            signals=np.array([]),
            equity_curve=equity,
            positions=np.array([]),
            sharpe_is=float(report["sharpe_is"]),
            sharpe_oos=float(report["sharpe_oos"]),
            ic_series=np.array([]),
            ic_mean=float(report["ic_mean"]),
            ic_std=float(report["ic_std"]),
            ic_tstat=float(report["ic_tstat"]),
            ic_pvalue=float(report["ic_pvalue"]),
            ic_halflife=int(report["ic_halflife"]),
            sortino=float(report["sortino"]),
            cvar_5pct=float(report["cvar_5pct"]),
            turnover=float(report["turnover"]),
            max_drawdown=float(report["max_drawdown"]),
            regime_metrics=report.get("regime_metrics", {}),
            capacity_estimate=float(report.get("capacity_estimate", 0)),
            run_id=report["run_id"],
            config_hash=report["config_hash"],
            latency_profile=config_data.get("latency_profile", {}),
            engine_type=report["engine_type"],
            fill_model=report["fill_model"],
            cost_model=report["cost_model"],
            instrument=report["instrument"],
            data_period=report["data_period"],
            data_source=report["data_source"],
            pipeline_mode=report["pipeline_mode"],
            created_at=report["created_at"],
            queue_fraction=report.get("queue_fraction"),
            maker_scorecard=report.get("maker_scorecard"),
            per_spread_breakdown=report.get("per_spread_breakdown"),
            daily_pnl=report.get("daily_pnl"),
        )

    def query(self, **filters: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if not self._base.exists():
            return results
        for run_dir in sorted(self._base.iterdir()):
            report_path = run_dir / "backtest_report.json"
            if not report_path.exists():
                continue
            report = json.loads(report_path.read_text())
            match = all(report.get(k) == v for k, v in filters.items())
            if match:
                results.append(report)
        return results
