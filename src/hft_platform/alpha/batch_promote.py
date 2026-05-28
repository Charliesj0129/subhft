"""Batch promotion orchestrator — run promote_alpha() across multiple alphas.

Discovers alphas with valid scorecards, runs the promotion pipeline for each,
and collects a pass/fail report.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from structlog import get_logger

from hft_platform.alpha.experiments import ExperimentTracker
from hft_platform.alpha.promotion import PromotionConfig, promote_alpha

logger = get_logger("alpha.batch_promote")


class BatchPromoter:
    """Run promotion pipeline across multiple alphas."""

    __slots__ = (
        "_experiments_dir",
        "_project_root",
        "_owner",
        "_min_sharpe_oos",
        "_max_abs_drawdown",
        "_max_correlation",
        "_validation_profile",
    )

    def __init__(
        self,
        *,
        experiments_dir: str | Path = "research/experiments",
        project_root: str | Path = ".",
        owner: str = "batch",
        min_sharpe_oos: float = 1.0,
        max_abs_drawdown: float = 0.2,
        max_correlation: float = 0.7,
        validation_profile: Any | None = None,
    ) -> None:
        self._experiments_dir = str(experiments_dir)
        self._project_root = str(project_root)
        self._owner = str(owner)
        self._min_sharpe_oos = float(min_sharpe_oos)
        self._max_abs_drawdown = float(max_abs_drawdown)
        self._max_correlation = float(max_correlation)
        self._validation_profile = validation_profile

    def run_fleet(
        self,
        *,
        dry_run: bool = True,
        top_n: int = 50,
        alpha_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Run promotion pipeline for multiple alphas.

        Parameters
        ----------
        dry_run : bool
            If True, evaluate but do not write promotion configs.
        top_n : int
            Max number of alphas to process (sorted by Sharpe).
        alpha_ids : list[str] | None
            If provided, only promote these alphas.

        Returns
        -------
        list[dict]
            Per-alpha result dicts with alpha_id, approved, gate_results, error.
        """
        tracker = ExperimentTracker(base_dir=self._experiments_dir)
        runs = tracker.list_runs()

        # Latest run per alpha (list_runs returns newest first)
        latest: dict[str, Any] = {}
        for run in runs:
            if run.alpha_id in latest:
                continue
            latest[run.alpha_id] = run

        # Filter to requested alpha_ids if specified
        if alpha_ids:
            target_set = set(alpha_ids)
            latest = {k: v for k, v in latest.items() if k in target_set}

        # Pre-filter by scorecard availability and sort by Sharpe
        candidates: list[tuple[str, Any, dict[str, Any]]] = []
        for alpha_id, run in latest.items():
            scorecard_path = Path(run.scorecard_path)
            if not scorecard_path.exists():
                continue
            try:
                scorecard = json.loads(scorecard_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue

            candidates.append((alpha_id, run, scorecard))

        candidates.sort(
            key=lambda c: float(c[2].get("sharpe_oos", 0.0)),
            reverse=True,
        )
        candidates = candidates[:top_n]

        results: list[dict[str, Any]] = []

        for alpha_id, run, scorecard in candidates:
            logger.info("batch_promote.evaluating", alpha_id=alpha_id)

            try:
                config = PromotionConfig(
                    alpha_id=alpha_id,
                    owner=self._owner,
                    project_root=self._project_root,
                    experiments_dir=self._experiments_dir,
                    scorecard_path=str(run.scorecard_path),
                    min_sharpe_oos=self._min_sharpe_oos,
                    max_abs_drawdown=self._max_abs_drawdown,
                    max_correlation=self._max_correlation,
                    write_promotion_config=not dry_run,
                    force=False,
                    validation_profile=self._validation_profile,
                )
                result = promote_alpha(config)
                results.append(
                    {
                        "alpha_id": alpha_id,
                        "approved": result.approved,
                        "dry_run": dry_run,
                        "scorecard_path": str(run.scorecard_path),
                        "sharpe_oos": float(scorecard.get("sharpe_oos", 0.0)),
                        "details": result.to_dict(),
                    }
                )
                logger.info(
                    "batch_promote.result",
                    alpha_id=alpha_id,
                    approved=result.approved,
                )
            except Exception as exc:
                logger.warning(
                    "batch_promote.error",
                    alpha_id=alpha_id,
                    error=str(exc),
                )
                results.append(
                    {
                        "alpha_id": alpha_id,
                        "approved": False,
                        "dry_run": dry_run,
                        "error": str(exc),
                    }
                )

        approved_count = sum(1 for r in results if r.get("approved"))
        logger.info(
            "batch_promote.complete",
            total=len(results),
            approved=approved_count,
            rejected=len(results) - approved_count,
        )
        return results
