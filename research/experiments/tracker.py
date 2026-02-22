from __future__ import annotations

from pathlib import Path

from hft_platform.alpha.experiments import ExperimentRun, ExperimentTracker


def ensure_experiment_layout(base_dir: str | Path = "research/experiments") -> Path:
    root = Path(base_dir)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    (root / "comparisons").mkdir(parents=True, exist_ok=True)
    return root


__all__ = ["ExperimentRun", "ExperimentTracker", "ensure_experiment_layout"]
