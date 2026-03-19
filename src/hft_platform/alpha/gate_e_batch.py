"""Gate E Candidate Discovery & Batch Orchestrator.

Scans ``research/experiments/promotions/`` for alphas that have passed Gate D
but not yet Gate E, then optionally drives them through a campaign runner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger("alpha.gate_e_batch")


def discover_gate_e_candidates(project_root: Path) -> list[dict[str, Any]]:
    """Scan promotions directory for Gate-D-passed, Gate-E-pending alphas.

    Walks ``research/experiments/promotions/<alpha_id>/<timestamp_hash>/``
    subdirectories and loads each ``promotion_decision.json``.  A candidate is
    included when ``gate_d_passed=True`` and ``gate_e_passed=False``.

    Args:
        project_root: Absolute path to the HFT platform repository root.

    Returns:
        List of dicts, each containing at minimum:
        ``alpha_id``, ``decision_path``, ``gate_d_passed``, ``gate_e_passed``.
        Invalid or unreadable JSON files are skipped with a warning log.
    """
    promotions_root = project_root / "research" / "experiments" / "promotions"
    candidates: list[dict[str, Any]] = []

    if not promotions_root.exists():
        logger.info("promotions_root_missing", path=str(promotions_root))
        return candidates

    # Structure: promotions/<alpha_id>/<timestamp_hash>/promotion_decision.json
    for decision_path in sorted(promotions_root.glob("*/*/promotion_decision.json")):
        try:
            raw = decision_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "gate_e_batch.skip_invalid_json",
                path=str(decision_path),
                error=str(exc),
            )
            continue

        if not isinstance(data, dict):
            logger.warning(
                "gate_e_batch.skip_non_dict",
                path=str(decision_path),
            )
            continue

        gate_d_passed = bool(data.get("gate_d_passed", False))
        gate_e_passed = bool(data.get("gate_e_passed", False))

        if gate_d_passed and not gate_e_passed:
            alpha_id = str(data.get("alpha_id", decision_path.parts[-3]))
            candidate: dict[str, Any] = {
                "alpha_id": alpha_id,
                "decision_path": str(decision_path),
                "gate_d_passed": gate_d_passed,
                "gate_e_passed": gate_e_passed,
            }
            # Preserve any extra fields from the JSON for downstream consumers.
            for key, value in data.items():
                if key not in candidate:
                    candidate[key] = value

            candidates.append(candidate)
            logger.debug(
                "gate_e_batch.candidate_found",
                alpha_id=alpha_id,
                decision_path=str(decision_path),
            )

    logger.info("gate_e_batch.discovery_complete", candidate_count=len(candidates))
    return candidates


@dataclass(frozen=True, slots=True)
class GateEBatchConfig:
    """Configuration for a Gate E batch run."""

    project_root: Path
    dry_run: bool = False
    max_concurrent: int = 4


@dataclass(frozen=True, slots=True)
class GateEBatchReport:
    """Summary report produced by :class:`GateEBatchRunner`."""

    candidates: tuple[dict[str, Any], ...]
    completed: tuple[str, ...]  # alpha_ids successfully processed
    failed: tuple[str, ...]  # alpha_ids that raised an error
    skipped: tuple[str, ...]  # alpha_ids skipped (e.g. no campaign runner)


class GateEBatchRunner:
    """Discovers Gate-E candidates and optionally drives them through a campaign.

    Args:
        campaign_runner: Optional callable / object used to run a paper-trade
            campaign for a given alpha.  When provided it is called as
            ``campaign_runner(alpha_id)`` for each candidate.  When *None*,
            candidates are recorded as *skipped*.
    """

    def __init__(self, campaign_runner: Any = None) -> None:
        self._campaign_runner = campaign_runner

    def run(self, config: GateEBatchConfig) -> GateEBatchReport:
        """Execute the batch discovery and optional campaign dispatch.

        In *dry-run* mode the report lists all candidates with empty
        ``completed``, ``failed``, and ``skipped`` tuples (no side-effects).

        In *run* mode each candidate is dispatched to ``campaign_runner`` if
        one was provided; otherwise it is recorded as *skipped*.

        The report is serialised to
        ``research/experiments/gate_e_batch_report.json`` under *project_root*.

        Args:
            config: Batch configuration.

        Returns:
            :class:`GateEBatchReport` summarising the run.
        """
        candidates = discover_gate_e_candidates(config.project_root)

        log = logger.bind(
            dry_run=config.dry_run,
            candidate_count=len(candidates),
            max_concurrent=config.max_concurrent,
        )
        log.info("gate_e_batch.run_started")

        if config.dry_run:
            log.info("gate_e_batch.dry_run_complete")
            report = GateEBatchReport(
                candidates=tuple(candidates),
                completed=(),
                failed=(),
                skipped=(),
            )
            self._write_report(config.project_root, report)
            return report

        completed: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []

        for candidate in candidates:
            alpha_id: str = candidate["alpha_id"]

            if self._campaign_runner is None:
                logger.info(
                    "gate_e_batch.skipped_no_runner",
                    alpha_id=alpha_id,
                )
                skipped.append(alpha_id)
                continue

            try:
                self._campaign_runner(alpha_id)
                logger.info("gate_e_batch.completed", alpha_id=alpha_id)
                completed.append(alpha_id)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "gate_e_batch.failed",
                    alpha_id=alpha_id,
                    error=str(exc),
                )
                failed.append(alpha_id)

        report = GateEBatchReport(
            candidates=tuple(candidates),
            completed=tuple(completed),
            failed=tuple(failed),
            skipped=tuple(skipped),
        )

        self._write_report(config.project_root, report)
        log.info(
            "gate_e_batch.run_complete",
            completed=len(completed),
            failed=len(failed),
            skipped=len(skipped),
        )
        return report

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_report(project_root: Path, report: GateEBatchReport) -> None:
        """Serialise *report* to ``research/experiments/gate_e_batch_report.json``."""
        output_dir = project_root / "research" / "experiments"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "gate_e_batch_report.json"

        payload: dict[str, Any] = {
            "candidates": list(report.candidates),
            "completed": list(report.completed),
            "failed": list(report.failed),
            "skipped": list(report.skipped),
        }
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.debug("gate_e_batch.report_written", path=str(output_path))
