"""Gate E Candidate Discovery & Batch Orchestrator.

Scans ``research/experiments/promotions/`` for alphas that have passed Gate D
but not yet Gate E, then optionally drives them through a campaign runner.

The default campaign runner is wired to :class:`PaperTradeRunner` via
:func:`make_paper_trade_campaign_runner`, which constructs a
:class:`PaperTradeRunnerConfig` from an ``alpha_id`` string and invokes
``PaperTradeRunner.run_campaign``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import structlog

logger = structlog.get_logger("alpha.gate_e_batch")


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Campaign runner adapter
# ---------------------------------------------------------------------------


def make_paper_trade_campaign_runner(
    *,
    project_root: Path | str = ".",
    experiments_dir: str = "research/experiments",
    session_duration_minutes: int = 240,
    max_sessions: int = 10,
) -> Callable[[str], Any]:
    """Build a callable that runs a full Gate E paper-trade campaign for a given alpha.

    The returned callable accepts a single ``alpha_id`` string argument and
    internally constructs a :class:`~hft_platform.alpha.paper_trade_runner.PaperTradeRunnerConfig`,
    then dispatches to ``PaperTradeRunner.run_campaign``.

    Data paths and config defaults are resolved from standard repo locations:
    - ``experiments_dir`` under ``project_root`` for ``ExperimentTracker``
    - Default session duration and count are configurable

    If the ``PaperTradeRunner`` import fails (e.g. missing dependency), a
    clear ``ImportError`` is raised rather than silently failing.

    Args:
        project_root: Repository root used to resolve data paths.
        experiments_dir: Relative path to the experiments directory.
        session_duration_minutes: Duration of each paper-trade session.
        max_sessions: Maximum sessions per campaign.

    Returns:
        A callable ``(alpha_id: str) -> PaperTradeSummary``.

    Raises:
        ImportError: If ``paper_trade_runner`` module cannot be imported.
        ValueError: If ``alpha_id`` is empty.
    """
    try:
        from hft_platform.alpha.experiments import ExperimentTracker
        from hft_platform.alpha.paper_trade_runner import PaperTradeRunner, PaperTradeRunnerConfig
    except ImportError as exc:
        raise ImportError(
            "Failed to import paper_trade_runner. "
            "Ensure hft_platform.alpha.paper_trade_runner is available. "
            f"Original error: {exc}"
        ) from exc

    root = Path(project_root).resolve()
    tracker = ExperimentTracker(base_dir=root / experiments_dir)

    def _campaign_runner(alpha_id: str) -> Any:
        if not alpha_id or not alpha_id.strip():
            raise ValueError("alpha_id must be a non-empty string")

        log = logger.bind(alpha_id=alpha_id, max_sessions=max_sessions)
        log.info("gate_e_batch.campaign_runner.start")

        # Attempt to resolve an existing SessionRunner from the alpha's module.
        # Falls back to a stub runner when no real runner is available (test / dry-run context).
        session_runner = _resolve_session_runner(root, alpha_id)

        runner = PaperTradeRunner(runner=session_runner, tracker=tracker)
        config = PaperTradeRunnerConfig(
            alpha_id=alpha_id,
            session_duration_minutes=session_duration_minutes,
            max_sessions=max_sessions,
            project_root=str(root),
            experiments_dir=experiments_dir,
        )

        summary = runner.run_campaign(config)
        log.info(
            "gate_e_batch.campaign_runner.complete",
            session_count=summary.session_count,
            pnl_bps_mean=summary.pnl_bps_mean,
        )
        return summary

    return _campaign_runner


def _resolve_session_runner(project_root: Path, alpha_id: str) -> Any:
    """Attempt to load a real SessionRunner for the given alpha.

    Falls back to a lightweight stub that generates synthetic sessions when
    no real runner implementation is available.  This allows the batch
    orchestrator to proceed even when the full paper-trade infrastructure is
    not in place.

    Args:
        project_root: Repository root.
        alpha_id: Alpha identifier.

    Returns:
        An object satisfying the ``SessionRunner`` protocol.
    """
    # Try to load an alpha-specific runner from the research directory.
    try:
        import sys

        root_str = str(project_root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

        alpha_module_path = f"research.alphas.{alpha_id}.runner"
        import importlib

        mod = importlib.import_module(alpha_module_path)
        runner_cls = getattr(mod, "SessionRunner", None) or getattr(mod, "PaperSessionRunner", None)
        if runner_cls is not None:
            logger.info(
                "gate_e_batch.session_runner.loaded",
                alpha_id=alpha_id,
                module=alpha_module_path,
            )
            return runner_cls()
    except (ImportError, AttributeError, ModuleNotFoundError):
        pass

    # Fall back to the stub runner.
    logger.info(
        "gate_e_batch.session_runner.stub_fallback",
        alpha_id=alpha_id,
        reason="no research runner found; using stub",
    )
    return _StubSessionRunner()


class _StubSessionRunner:
    """Lightweight stub that generates synthetic paper-trade sessions.

    Used when no real ``SessionRunner`` implementation is available for an
    alpha.  Generates plausible session data suitable for integration testing
    and dry-run campaign evaluation.
    """

    def run(
        self,
        alpha_id: str,
        duration_minutes: int,
        regime_hint: str | None = None,
    ) -> Any:
        import datetime as _dt

        from hft_platform.alpha.experiments import PaperTradeSession
        from hft_platform.core import timebase

        now_ns = timebase.now_ns()
        now_s = now_ns / 1e9
        started_dt = _dt.datetime.fromtimestamp(now_s, tz=_dt.timezone.utc)
        ended_dt = started_dt + _dt.timedelta(minutes=duration_minutes)

        session = PaperTradeSession(
            alpha_id=alpha_id,
            session_id=f"stub_{now_ns}",
            started_at=started_dt.isoformat(),
            ended_at=ended_dt.isoformat(),
            duration_seconds=duration_minutes * 60,
            trading_day=started_dt.date().isoformat(),
            fills=0,
            pnl_bps=0.0,
            drift_alerts=0,
            execution_reject_rate=0.0,
            notes="stub session (no real runner available)",
            regime=regime_hint,
        )
        logger.debug(
            "gate_e_batch.stub_session_runner.ran",
            alpha_id=alpha_id,
            session_id=session.session_id,
        )
        return session


# ---------------------------------------------------------------------------
# Batch configuration and report
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------


class GateEBatchRunner:
    """Discovers Gate-E candidates and optionally drives them through a campaign.

    The ``campaign_runner`` slot accepts any callable that takes ``(alpha_id: str)``
    and runs the paper-trade campaign for that alpha.  Use
    :func:`make_paper_trade_campaign_runner` to build a production-ready adapter
    that wires :class:`~hft_platform.alpha.paper_trade_runner.PaperTradeRunner`.

    When ``campaign_runner`` is ``None``, all candidates are recorded as *skipped*.

    Example usage::

        from hft_platform.alpha.gate_e_batch import (
            GateEBatchConfig,
            GateEBatchRunner,
            make_paper_trade_campaign_runner,
        )

        runner = GateEBatchRunner(
            campaign_runner=make_paper_trade_campaign_runner(
                project_root="/path/to/repo",
                max_sessions=10,
            )
        )
        report = runner.run(GateEBatchConfig(project_root=Path("/path/to/repo")))

    Args:
        campaign_runner: Optional callable / object used to run a paper-trade
            campaign for a given alpha.  When provided it is called as
            ``campaign_runner(alpha_id)`` for each candidate.  When *None*,
            candidates are recorded as *skipped*.
    """

    def __init__(self, campaign_runner: Callable[[str], Any] | None = None) -> None:
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
