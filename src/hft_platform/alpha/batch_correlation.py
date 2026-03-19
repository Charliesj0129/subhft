from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from structlog import get_logger

from hft_platform.alpha.experiments import ExperimentTracker

logger = get_logger("alpha.batch_correlation")


def _max_pool_correlation(
    signal: np.ndarray,
    pool_signals: dict[str, np.ndarray],
    exclude_id: str,
) -> float:
    """Max absolute Pearson correlation between signal and pool (excluding self)."""
    max_corr = 0.0
    sig = np.asarray(signal, dtype=np.float64)
    if sig.size < 2:
        return 0.0
    for other_id, other_sig in pool_signals.items():
        if other_id == exclude_id:
            continue
        other = np.asarray(other_sig, dtype=np.float64)
        min_len = min(sig.size, other.size)
        if min_len < 2:
            continue
        a, b = sig[:min_len], other[:min_len]
        std_a, std_b = float(np.std(a)), float(np.std(b))
        if std_a < 1e-12 or std_b < 1e-12:
            continue
        corr = float(np.abs(np.corrcoef(a, b)[0, 1]))
        if corr > max_corr:
            max_corr = corr
    return max_corr


def batch_compute_correlations(
    experiments_dir: str | Path = "research/experiments",
    project_root: str | Path = ".",
    *,
    dry_run: bool = False,
) -> dict[str, float]:
    """Compute correlation_pool_max for all alphas and patch scorecards.

    Returns dict mapping alpha_id -> correlation_pool_max.
    """
    tracker = ExperimentTracker(base_dir=experiments_dir)
    pool_signals = tracker.latest_signals_by_alpha()

    if not pool_signals:
        logger.info("batch_correlation: no signals found")
        return {}

    results: dict[str, float] = {}
    runs = tracker.list_runs()
    # Group runs by alpha_id, keep only latest per alpha
    latest_runs: dict[str, Any] = {}
    for run in runs:
        if run.alpha_id not in latest_runs:
            latest_runs[run.alpha_id] = run

    for alpha_id, signal in pool_signals.items():
        corr = _max_pool_correlation(signal, pool_signals, alpha_id)
        results[alpha_id] = corr

        if dry_run:
            logger.info(
                "batch_correlation.dry_run",
                alpha_id=alpha_id,
                correlation_pool_max=round(corr, 6),
            )
            continue

        # Patch scorecard if we have the run
        run = latest_runs.get(alpha_id)
        if run is None:
            continue
        scorecard_path = Path(run.scorecard_path)
        if not scorecard_path.exists():
            logger.warning(
                "batch_correlation: scorecard not found",
                alpha_id=alpha_id,
                path=str(scorecard_path),
            )
            continue

        try:
            scorecard = json.loads(scorecard_path.read_text())
            scorecard["correlation_pool_max"] = round(corr, 6)
            scorecard_path.write_text(
                json.dumps(scorecard, indent=2, sort_keys=True)
            )
            logger.info(
                "batch_correlation.patched",
                alpha_id=alpha_id,
                correlation_pool_max=round(corr, 6),
            )
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "batch_correlation: failed to patch scorecard",
                alpha_id=alpha_id,
                error=str(exc),
            )

    return results
