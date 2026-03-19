"""Batch signal screening for alpha discovery pipeline.

Runs ``screener.run_screen`` across multiple alphas in sequence,
collecting results into a summary report.  Used by the
``hft alpha batch-screen`` CLI command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from hft_platform.alpha._validation_types import ScreenConfig
from hft_platform.alpha.screener import ScreenResult, run_screen

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BatchScreenSummary:
    """Summary of a batch screening run."""

    total: int
    passed: int
    failed: int
    errors: int
    results: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "results": dict(self.results),
        }


def run_batch_screen(
    alpha_ids: list[str],
    data_paths: list[str],
    *,
    project_root: str = ".",
    experiments_dir: str = "research/experiments",
    min_ic: float = 0.005,
    min_sharpe_oos: float = -0.5,
) -> BatchScreenSummary:
    """Screen multiple alphas and return a summary.

    Args:
        alpha_ids: List of alpha IDs to screen.
        data_paths: Data paths for backtesting.
        project_root: Project root directory.
        experiments_dir: Experiments directory.
        min_ic: Minimum IC threshold.
        min_sharpe_oos: Minimum Sharpe OOS threshold.

    Returns:
        BatchScreenSummary with per-alpha results.
    """
    results: dict[str, dict[str, Any]] = {}
    passed = 0
    failed = 0
    errors = 0

    for alpha_id in alpha_ids:
        try:
            config = ScreenConfig(
                alpha_id=alpha_id,
                data_paths=data_paths,
                project_root=project_root,
                experiments_dir=experiments_dir,
                min_ic=min_ic,
                min_sharpe_oos=min_sharpe_oos,
            )
            result = run_screen(config)
            results[alpha_id] = result.to_dict()
            if result.screen_passed:
                passed += 1
            else:
                failed += 1
        except Exception as exc:
            _log.warning("batch_screen_error", alpha_id=alpha_id, error=str(exc))
            results[alpha_id] = {"error": str(exc), "screen_passed": False}
            errors += 1

    _log.info(
        "batch_screen_complete",
        total=len(alpha_ids),
        passed=passed,
        failed=failed,
        errors=errors,
    )

    return BatchScreenSummary(
        total=len(alpha_ids),
        passed=passed,
        failed=failed,
        errors=errors,
        results=results,
    )
