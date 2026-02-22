"""Alpha experiment tracking — log runs, compare metrics, load signal/equity arrays.

Each run is stored as a directory under ``base_dir/runs/<run_id>/`` containing:
  - ``meta.json``:             ExperimentRun metadata
  - ``scorecard.json``:        gate-C scorecard payload
  - ``backtest_report.json``:  full backtest report dict
  - ``signals.npy`` (opt):     signal array (float64)
  - ``equity.npy``   (opt):    equity-curve array (float64)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from structlog import get_logger

logger = get_logger("alpha.experiments")


@dataclass(frozen=True)
class ExperimentRun:
    run_id: str
    alpha_id: str
    config_hash: str
    timestamp: str
    data_paths: tuple[str, ...]
    metrics: dict[str, float]
    gate_status: dict[str, bool]
    scorecard_path: str
    backtest_report_path: str
    signals_path: str | None = None
    equity_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExperimentTracker:
    def __init__(self, base_dir: str | Path = "research/experiments"):
        self.base_dir = Path(base_dir)
        self.runs_dir = self.base_dir / "runs"
        self.comparisons_dir = self.base_dir / "comparisons"

    def log_run(
        self,
        *,
        run_id: str,
        alpha_id: str,
        config_hash: str,
        data_paths: list[str],
        metrics: dict[str, float],
        gate_status: dict[str, bool],
        scorecard_payload: dict[str, Any],
        backtest_report_payload: dict[str, Any],
        signals: np.ndarray | None = None,
        equity: np.ndarray | None = None,
    ) -> Path:
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        scorecard_path = run_dir / "scorecard.json"
        backtest_report_path = run_dir / "backtest_report.json"
        scorecard_path.write_text(json.dumps(scorecard_payload, indent=2, sort_keys=True))
        backtest_report_path.write_text(json.dumps(backtest_report_payload, indent=2, sort_keys=True))

        signals_path: Path | None = None
        equity_path: Path | None = None
        if signals is not None:
            signals_path = run_dir / "signals.npy"
            np.save(signals_path, np.asarray(signals, dtype=np.float64))
        if equity is not None:
            equity_path = run_dir / "equity.npy"
            np.save(equity_path, np.asarray(equity, dtype=np.float64))

        meta = ExperimentRun(
            run_id=run_id,
            alpha_id=alpha_id,
            config_hash=config_hash,
            timestamp=datetime.now(UTC).isoformat(),
            data_paths=tuple(str(p) for p in data_paths),
            metrics=dict(metrics),
            gate_status=dict(gate_status),
            scorecard_path=str(scorecard_path),
            backtest_report_path=str(backtest_report_path),
            signals_path=(str(signals_path) if signals_path else None),
            equity_path=(str(equity_path) if equity_path else None),
        )
        meta_path = run_dir / "meta.json"
        meta_path.write_text(json.dumps(meta.to_dict(), indent=2, sort_keys=True))
        return meta_path

    def list_runs(self, alpha_id: str | None = None) -> list[ExperimentRun]:
        rows: list[ExperimentRun] = []
        for meta_path in sorted(self.runs_dir.glob("*/meta.json")):
            try:
                payload = json.loads(meta_path.read_text())
                row = _from_dict(payload)
            except (OSError, ValueError, KeyError) as exc:
                logger.warning("experiments.list_runs: skipping corrupt meta", path=str(meta_path), error=str(exc))
                continue
            if alpha_id and row.alpha_id != alpha_id:
                continue
            rows.append(row)
        rows.sort(key=lambda item: item.timestamp, reverse=True)
        return rows

    def compare(self, run_ids: list[str]) -> list[dict[str, Any]]:
        target = set(run_ids)
        out: list[dict[str, Any]] = []
        for row in self.list_runs():
            if row.run_id not in target:
                continue
            out.append(
                {
                    "run_id": row.run_id,
                    "alpha_id": row.alpha_id,
                    "config_hash": row.config_hash,
                    "timestamp": row.timestamp,
                    **row.metrics,
                }
            )
        return sorted(out, key=lambda item: run_ids.index(item["run_id"])) if out else []

    def best_by_metric(
        self,
        metric: str,
        n: int = 10,
        alpha_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.list_runs(alpha_id=alpha_id)
        scored: list[dict[str, Any]] = []
        for row in rows:
            value = row.metrics.get(metric)
            if value is None:
                continue
            scored.append(
                {
                    "run_id": row.run_id,
                    "alpha_id": row.alpha_id,
                    "metric": metric,
                    "value": float(value),
                    "timestamp": row.timestamp,
                    "config_hash": row.config_hash,
                }
            )
        scored.sort(key=lambda item: item["value"], reverse=True)
        return scored[: max(1, n)]

    def latest_signals_by_alpha(self) -> dict[str, np.ndarray]:
        latest: dict[str, ExperimentRun] = {}
        for row in self.list_runs():
            if row.alpha_id in latest:
                continue
            latest[row.alpha_id] = row

        signals: dict[str, np.ndarray] = {}
        for alpha_id, row in latest.items():
            if not row.signals_path:
                continue
            arr = _load_numpy(row.signals_path)
            if arr is None:
                continue
            signals[alpha_id] = np.asarray(arr, dtype=np.float64)
        return signals

    def latest_equity_by_alpha(self) -> dict[str, np.ndarray]:
        latest: dict[str, ExperimentRun] = {}
        for row in self.list_runs():
            if row.alpha_id in latest:
                continue
            latest[row.alpha_id] = row

        equities: dict[str, np.ndarray] = {}
        for alpha_id, row in latest.items():
            if not row.equity_path:
                continue
            arr = _load_numpy(row.equity_path)
            if arr is None:
                continue
            equities[alpha_id] = np.asarray(arr, dtype=np.float64)
        return equities

    def proxy_returns(self) -> np.ndarray | None:
        equities = self.latest_equity_by_alpha()
        if not equities:
            return None

        rows: list[np.ndarray] = []
        for eq in equities.values():
            arr = np.asarray(eq, dtype=np.float64)
            if arr.size < 2:
                continue
            prev = arr[:-1]
            delta = np.diff(arr)
            ret = np.divide(delta, prev, out=np.zeros_like(delta), where=np.abs(prev) > 1e-12)
            rows.append(np.nan_to_num(ret, nan=0.0, posinf=0.0, neginf=0.0))

        if not rows:
            return None

        min_len = min(row.size for row in rows)
        if min_len < 2:
            return None
        data = np.vstack([row[:min_len] for row in rows])
        proxy = np.nanmedian(data, axis=0)
        return np.asarray(proxy, dtype=np.float64)


def _from_dict(payload: dict[str, Any]) -> ExperimentRun:
    return ExperimentRun(
        run_id=str(payload["run_id"]),
        alpha_id=str(payload["alpha_id"]),
        config_hash=str(payload.get("config_hash", "")),
        timestamp=str(payload.get("timestamp", "")),
        data_paths=tuple(payload.get("data_paths", ())),
        metrics={str(k): float(v) for k, v in dict(payload.get("metrics", {})).items()},
        gate_status={str(k): bool(v) for k, v in dict(payload.get("gate_status", {})).items()},
        scorecard_path=str(payload.get("scorecard_path", "")),
        backtest_report_path=str(payload.get("backtest_report_path", "")),
        signals_path=str(payload["signals_path"]) if payload.get("signals_path") else None,
        equity_path=str(payload["equity_path"]) if payload.get("equity_path") else None,
    )


def _load_numpy(path_str: str) -> np.ndarray | None:
    path = Path(path_str)
    if not path.exists():
        return None
    try:
        arr = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as exc:
        logger.warning("experiments._load_numpy: failed to load", path=str(path), error=str(exc))
        return None
    return np.asarray(arr, dtype=np.float64)
