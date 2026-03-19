"""Gate E batch runner — discover and evaluate Gate E candidates in bulk.

Scans research/experiments/runs/ for alpha experiments that have passed
Gate D and are ready for Gate E paper-trade governance evaluation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from hft_platform.alpha._gate_e import _evaluate_gate_e
from hft_platform.alpha._promotion_types import PromotionConfig

logger = structlog.get_logger("alpha.gate_e_batch")


@dataclass(frozen=True, slots=True)
class GateEBatchConfig:
    project_root: str = "."
    dry_run: bool = False
    min_shadow_sessions: int = 5
    max_execution_reject_rate: float = 0.01
    require_paper_trade_governance: bool = False
    owner: str = "batch"


@dataclass(frozen=True, slots=True)
class GateEBatchReport:
    total_candidates: int
    passed: int
    failed: int
    skipped: int
    results: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_candidates": self.total_candidates,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "results": list(self.results),
        }


def discover_gate_e_candidates(project_root: Path) -> list[tuple[str, Path]]:
    """Scan research/experiments/runs/ for Gate D-passed alpha experiments.

    Returns list of (alpha_id, run_dir) pairs for experiments where
    gate_status.gate_d is True.
    """
    runs_dir = project_root / "research" / "experiments" / "runs"
    if not runs_dir.exists():
        logger.info("gate_e_batch.no_runs_dir", path=str(runs_dir))
        return []

    candidates: list[tuple[str, Path]] = []
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        meta_path = run_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
            gate_status = meta.get("gate_status") or {}
            if bool(gate_status.get("gate_d")):
                alpha_id = str(meta.get("alpha_id", run_dir.name))
                candidates.append((alpha_id, run_dir))
        except Exception as exc:
            logger.warning(
                "gate_e_batch.meta_read_error",
                path=str(meta_path),
                error=str(exc),
            )
    return candidates


class GateEBatchRunner:
    """Run Gate E evaluation for a batch of alpha candidates."""

    def __init__(self, config: GateEBatchConfig) -> None:
        self._config = config
        self._project_root = Path(config.project_root).resolve()

    def run(self) -> GateEBatchReport:
        """Discover and evaluate all Gate E candidates."""
        candidates = discover_gate_e_candidates(self._project_root)
        logger.info(
            "gate_e_batch.start",
            candidates=len(candidates),
            dry_run=self._config.dry_run,
        )

        results: list[dict[str, Any]] = []
        passed = 0
        failed = 0
        skipped = 0

        for alpha_id, run_dir in candidates:
            result_entry = self._evaluate_candidate(alpha_id, run_dir)
            results.append(result_entry)
            if result_entry["skipped"]:
                skipped += 1
            elif result_entry["passed"]:
                passed += 1
            else:
                failed += 1

        report = GateEBatchReport(
            total_candidates=len(candidates),
            passed=passed,
            failed=failed,
            skipped=skipped,
            results=tuple(results),
        )
        logger.info(
            "gate_e_batch.complete",
            total=len(candidates),
            passed=passed,
            failed=failed,
            skipped=skipped,
        )
        return report

    def _evaluate_candidate(self, alpha_id: str, run_dir: Path) -> dict[str, Any]:
        if self._config.dry_run:
            logger.info("gate_e_batch.dry_run_skip", alpha_id=alpha_id)
            return {
                "alpha_id": alpha_id,
                "run_dir": str(run_dir),
                "passed": False,
                "skipped": True,
                "dry_run": True,
                "checks": {},
            }

        try:
            promo_config = PromotionConfig(
                alpha_id=alpha_id,
                owner=self._config.owner,
                project_root=str(self._project_root),
                min_shadow_sessions=self._config.min_shadow_sessions,
                max_execution_reject_rate=self._config.max_execution_reject_rate,
                require_paper_trade_governance=self._config.require_paper_trade_governance,
            )
            passed, result = _evaluate_gate_e(promo_config, self._project_root)
            return {
                "alpha_id": alpha_id,
                "run_dir": str(run_dir),
                "passed": passed,
                "skipped": False,
                "dry_run": False,
                "checks": result.get("checks", {}),
            }
        except Exception as exc:
            logger.error(
                "gate_e_batch.evaluation_error",
                alpha_id=alpha_id,
                error=str(exc),
            )
            return {
                "alpha_id": alpha_id,
                "run_dir": str(run_dir),
                "passed": False,
                "skipped": True,
                "error": str(exc),
                "checks": {},
            }
