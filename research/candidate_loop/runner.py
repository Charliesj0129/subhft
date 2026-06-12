"""Batch run orchestrator implementing the spec §5 data flow.

[1] ingest family JSONL files → NEW rows; [2] validate/compile → INVALID |
COMPILED; [3]+[4] outer loop over split days (ONE panel in memory, cached),
inner loop over candidates; [5] reduce per split (train/validation) →
experiment_results rows + Parquet artifacts → EVALUATED; [6] score/gate →
REJECTED | WATCHLIST | PROMOTED; [7] test-split pass for WATCHLIST+PROMOTED
only (recorded, never summarized); [8] failure_summary.json
(train+validation only).

Idempotency: re-running the same batch produces ZERO new rows — candidate
rows dedupe on ``(alpha_id, run_id, status)``, result rows on ``result_id``,
panels come from the cache, and the prior-run dedupe query EXCLUDES the
current ``run_id`` (otherwise a re-run would flag every candidate
``DUPLICATE_ALPHA`` against its own first pass). ``--resume`` is therefore
the same operation as ``run``.

Offline research CLI — float math and per-call allocation are fine here;
nothing in this module is on or near the hot path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from structlog import get_logger

from research.backtest.cost_models import load_cost_profile
from research.backtest.q_hat_table import QHatTable
from research.candidate_loop.artifacts import (
    DEFAULT_ARTIFACT_ROOT,
    panel_cache_dir,
    split_artifact_dir,
    write_split_artifacts,
)
from research.candidate_loop.ch_writer import ResultWriter, compute_result_id
from research.candidate_loop.evaluator import (
    DayEval,
    EvaluatorConfig,
    aggregate_split,
    evaluate_day,
    load_evaluator_config,
)
from research.candidate_loop.failure_summary import (
    build_failure_summary,
    write_failure_summary,
)
from research.candidate_loop.generate import (
    DEFAULT_CANDIDATES_ROOT,
    family_from_filename,
    read_family_jsonl,
)
from research.candidate_loop.panels import Panel, build_panel, fetch_dir_coverage
from research.candidate_loop.schema import (
    SCHEMA_VERSION,
    Status,
    canonical_json,
)
from research.candidate_loop.scoring import (
    GateOutcome,
    ScoredCandidate,
    ScoringConfig,
    apply_hard_gates,
    assign_statuses,
    compute_final_score,
    direction_match,
    load_scoring_config,
)
from research.candidate_loop.splits import (
    DaySymbol,
    SplitDefinition,
    load_split_definition,
    npz_path_for,
)
from research.candidate_loop.validator import (
    InvalidCandidate,
    ValidCandidate,
    ValidationResult,
    validate_batch,
)

logger = get_logger("candidate_loop_runner")

DEFAULT_CONFIG_DIR = Path("config/research/candidate_loop")
DEFAULT_RUNS_ROOT = Path("research/candidate_loop/runs")
DEFAULT_DATA_ROOT = Path("research/data/raw")

SUMMARY_SPLITS = ("train", "validation")

PRIOR_HASHES_SQL = (
    "SELECT DISTINCT formula_hash FROM research.alpha_candidates "
    "WHERE primitive_version = %(p)s AND run_id != %(r)s AND formula_hash != ''"
)


@dataclass(frozen=True)
class RunConfig:
    run_id: str
    batch_dir: Path
    run_dir: Path
    data_root: Path = DEFAULT_DATA_ROOT
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT
    evaluator_config_path: Path = DEFAULT_CONFIG_DIR / "evaluator_v1.yaml"
    scoring_config_path: Path = DEFAULT_CONFIG_DIR / "scoring_v1.yaml"
    split_definition_path: Path = DEFAULT_CONFIG_DIR / "split_definition_v1.yaml"

    @classmethod
    def for_run_id(
        cls,
        run_id: str,
        candidates_root: Path = DEFAULT_CANDIDATES_ROOT,
        runs_root: Path = DEFAULT_RUNS_ROOT,
        **overrides: Any,
    ) -> "RunConfig":
        return cls(
            run_id=run_id,
            batch_dir=candidates_root / run_id,
            run_dir=runs_root / run_id,
            **overrides,
        )


@dataclass
class _Tracked:
    """One ingested candidate line plus its per-family provenance."""

    family_file: str
    provenance: dict[str, str]
    result: ValidationResult
    # filled during evaluation
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    gate: GateOutcome | None = None
    final_score: float = 0.0
    status: Status = Status.NEW
    death_reason: str = ""
    detail: str = ""


def _pseudo_alpha_id(raw_json: str) -> str:
    """Stable id for lines that never decoded (alpha_id would be '')."""
    return hashlib.sha256(raw_json.encode("utf-8")).hexdigest()[:16]


def _sanitize_expected_sign(value: str) -> str:
    return value if value in ("positive", "negative") else "positive"


def _alpha_id_of(result: ValidationResult) -> str:
    if isinstance(result, ValidCandidate):
        return result.alpha_id
    return result.alpha_id or _pseudo_alpha_id(result.raw_json)


def fetch_prior_hashes(client: Any, primitive_version: str, run_id: str) -> frozenset[str]:
    """Cross-run dedupe pool; MUST exclude the current run_id (idempotency)."""
    if client is None:
        return frozenset()
    try:
        rows = client.query(
            PRIOR_HASHES_SQL, parameters={"p": primitive_version, "r": run_id}
        ).result_rows
    except Exception:  # noqa: BLE001 - degrade to within-batch dedupe only
        logger.warning("prior formula_hash query failed; within-batch dedupe only", exc_info=True)
        return frozenset()
    return frozenset(str(r[0]) for r in rows if r and r[0])


class BatchRunner:
    def __init__(
        self,
        rc: RunConfig,
        client: Any | None,
        *,
        eval_cfg: EvaluatorConfig | None = None,
        scoring_cfg: ScoringConfig | None = None,
        split_def: SplitDefinition | None = None,
    ) -> None:
        self.rc = rc
        self.client = client
        self.eval_cfg = eval_cfg or load_evaluator_config(rc.evaluator_config_path)
        self.scoring_cfg = scoring_cfg or load_scoring_config(rc.scoring_config_path)
        self.split_def = split_def or load_split_definition(rc.split_definition_path)
        self.writer = ResultWriter(client, rc.run_dir)
        self.q_hat = self._load_q_hat()
        self.cost_per_side_pts = self._resolve_cost_per_side()
        self.experiment_id = (
            f"{rc.run_id}:{self.eval_cfg.evaluator_version}:{self.scoring_cfg.scoring_version}"
        )
        self.versions = {
            "schema_version": SCHEMA_VERSION,
            "data_version": self.split_def.data_version,
            "primitive_version": self.eval_cfg.primitive_version,
            "evaluator_version": self.eval_cfg.evaluator_version,
            "scoring_version": self.scoring_cfg.scoring_version,
            "cost_assumption_version": self.eval_cfg.cost_assumption_version,
            "latency_config_version": self.eval_cfg.latency_config_version,
            "split_definition_version": self.split_def.split_definition_version,
            "maker_cost_assumption_version": self.scoring_cfg.maker_cost_assumption_version,
        }

    # -- setup ----------------------------------------------------------------

    def _load_q_hat(self) -> QHatTable:
        path = Path(self.scoring_cfg.q_hat_table_path)
        if path.exists():
            return QHatTable.load(path)
        logger.warning(
            "q_hat table missing; maker view degrades to fallback q_hat",
            path=str(path),
        )
        return QHatTable()

    def _resolve_cost_per_side(self) -> float:
        """Conservative (max) cost across every split symbol; logs non-uniform sets."""
        symbols = sorted({ds.symbol for _, ds in self.split_def.all_pairs()})
        costs: dict[str, float] = {}
        for sym in symbols:
            try:
                costs[sym] = load_cost_profile(sym).cost_per_side_pts
            except KeyError:
                logger.warning("no cost profile for split symbol", symbol=sym)
        if not costs:
            raise RuntimeError(f"No cost profile found for any split symbol: {symbols}")
        if len(set(costs.values())) > 1:
            logger.warning("non-uniform cost profiles across split symbols", costs=costs)
        return max(costs.values())

    # -- [1]+[2] ingest + validate ---------------------------------------------

    def ingest_and_validate(self) -> list[_Tracked]:
        files = sorted(self.rc.batch_dir.glob("family=*.jsonl"))
        if not files:
            raise FileNotFoundError(f"No family=*.jsonl files under {self.rc.batch_dir}")
        lines: list[str] = []
        contexts: list[tuple[str, dict[str, str]]] = []
        for path in files:
            family_from_filename(path)  # raises on malformed names
            header, file_lines = read_family_jsonl(path)
            provenance = {
                "generation_model": str((header or {}).get("generation_model", "")),
                "generation_prompt_id": str((header or {}).get("prompt_id", "")),
                "generation_run_id": str((header or {}).get("generation_run_id", "")),
            }
            for line in file_lines:
                lines.append(line)
                contexts.append((path.name, provenance))

        prior = fetch_prior_hashes(self.client, self.eval_cfg.primitive_version, self.rc.run_id)
        results = validate_batch(lines, prior)
        tracked = [
            _Tracked(family_file=ctx[0], provenance=ctx[1], result=res)
            for ctx, res in zip(contexts, results)
        ]
        for t in tracked:
            self._write_candidate_status(t, Status.NEW)
            if isinstance(t.result, InvalidCandidate):
                t.status = Status.INVALID
                t.death_reason = t.result.death_reason.value
                t.detail = t.result.detail
                self._write_candidate_status(t, Status.INVALID)
            else:
                t.status = Status.COMPILED
                self._write_candidate_status(t, Status.COMPILED)
        return tracked

    # -- candidate rows ----------------------------------------------------------

    def _candidate_row(self, t: _Tracked, status: Status) -> dict[str, Any]:
        res = t.result
        cand = res.candidate if isinstance(res, (ValidCandidate, InvalidCandidate)) else None
        row: dict[str, Any] = {
            "alpha_id": _alpha_id_of(res),
            "run_id": self.rc.run_id,
            "status": status.value,
            "death_reason": t.death_reason if status in (Status.INVALID, Status.REJECTED) else "",
            "candidate_json": canonical_json(cand) if cand is not None else res.raw_json,
            "formula_hash": res.formula_hash if isinstance(res, ValidCandidate) else "",
            "uses_trade_imbalance": int(res.uses_trade_imbalance)
            if isinstance(res, ValidCandidate)
            else 0,
            "data_version": self.split_def.data_version,
            "primitive_version": self.eval_cfg.primitive_version,
            "schema_version": SCHEMA_VERSION,
            **t.provenance,
        }
        if cand is not None:
            row.update(
                name=cand.name,
                family=cand.family,
                hypothesis=cand.hypothesis,
                feature_formulas=[f"{f.name}={f.formula}" for f in cand.features],
                signal_formula=cand.signal_formula,
                label=cand.label,
                horizon=cand.horizon,
                expected_sign=_sanitize_expected_sign(cand.expected_sign),
                regime_filter=cand.regime_filter,
                proposed_new_primitives=[p.name for p in cand.proposed_new_primitives],
            )
        else:
            row.update(name="", family="", expected_sign="positive")
        return row

    def _write_candidate_status(self, t: _Tracked, status: Status) -> None:
        self.writer.write_candidate_row(self._candidate_row(t, status))

    # -- [3]+[4] panels + per-day evaluation -------------------------------------

    def _load_panel(self, ds: DaySymbol) -> Panel | None:
        npz = npz_path_for(self.rc.data_root, ds)
        if not npz.exists():
            logger.warning("missing NPZ for split day", symbol=ds.symbol, day=ds.day)
            return None
        coverage: float | None = None
        source = "not_queried"
        if self.client is not None:
            coverage, source = fetch_dir_coverage(self.client, ds.symbol, ds.day)
        return build_panel(
            npz,
            ds.symbol,
            ds.day,
            self.eval_cfg.tick_size.get(ds.symbol, 1.0),
            panel_cache_dir(self.rc.artifact_root, self.split_def.data_version),
            dir_coverage=coverage,
            dir_coverage_source=source,
        )

    def _evaluate_split_days(
        self, candidates: list[_Tracked], split: str
    ) -> dict[str, list[DayEval]]:
        """Outer loop days, inner loop candidates; one panel in memory at a time."""
        day_evals: dict[str, list[DayEval]] = {
            _alpha_id_of(t.result): [] for t in candidates
        }
        for ds in self.split_def.splits[split]:
            panel = self._load_panel(ds)
            for t in candidates:
                valid = t.result
                assert isinstance(valid, ValidCandidate)
                if panel is None:
                    ev = DayEval(day=ds.day, symbol=ds.symbol, skipped_reason="missing_npz")
                else:
                    ev = evaluate_day(valid, panel, self.eval_cfg)
                day_evals[valid.alpha_id].append(ev)
        return day_evals

    # -- [5]+[6]+[7] reduce, gate, record -----------------------------------------

    def _aggregate(self, valid: ValidCandidate, day_evals: list[DayEval]) -> dict[str, Any]:
        return aggregate_split(
            day_evals,
            expected_sign=valid.candidate.expected_sign,
            cfg=self.eval_cfg,
            cost_per_side_pts=self.cost_per_side_pts,
            q_hat=self.q_hat,
            q_hat_symbol=self.scoring_cfg.q_hat_symbol,
        )

    def _result_row(
        self,
        t: _Tracked,
        split: str,
        metrics: dict[str, Any],
        day_evals: list[DayEval],
    ) -> dict[str, Any]:
        valid = t.result
        assert isinstance(valid, ValidCandidate)
        alpha_id = valid.alpha_id
        first, last = self.split_def.split_range(split)
        artifact_dir = split_artifact_dir(
            self.rc.artifact_root,
            self.split_def.data_version,
            self.eval_cfg.evaluator_version,
            alpha_id,
            split,
        )
        row: dict[str, Any] = {
            k: v for k, v in metrics.items() if not isinstance(v, (list, dict))
        }
        train_ic = float(t.metrics.get("train", {}).get("ic", 0.0))
        val_ic = float(t.metrics.get("validation", {}).get("ic", 0.0))
        test_ic = float(t.metrics.get("test", {}).get("ic", 0.0))
        row.update(
            result_id=compute_result_id(
                alpha_id,
                self.rc.run_id,
                split,
                self.split_def.data_version,
                self.eval_cfg.evaluator_version,
                self.scoring_cfg.scoring_version,
            ),
            experiment_id=self.experiment_id,
            run_id=self.rc.run_id,
            alpha_id=alpha_id,
            family=valid.candidate.family,
            split=split,
            split_start=first,
            split_end=last,
            train_validation_direction_match=int(direction_match(train_ic, val_ic)),
            validation_test_direction_match=int(direction_match(val_ic, test_ic))
            if split == "test"
            else 0,
            final_score=t.final_score,
            gates_passed=list(t.gate.gates_passed) if t.gate else [],
            gates_failed=list(t.gate.gates_failed) if t.gate else [],
            status=t.status.value,
            death_reason=t.death_reason,
            artifact_path=str(artifact_dir),
            data_version=self.split_def.data_version,
            primitive_version=self.eval_cfg.primitive_version,
            evaluator_version=self.eval_cfg.evaluator_version,
            scoring_version=self.scoring_cfg.scoring_version,
            cost_assumption_version=self.eval_cfg.cost_assumption_version,
            maker_cost_assumption_version=str(
                metrics.get("maker_cost_assumption_version", "")
            ),
            latency_config_version=self.eval_cfg.latency_config_version,
            **t.provenance,
        )
        write_split_artifacts(
            artifact_dir,
            day_evals,
            metrics={k: v for k, v in metrics.items() if k != "daily_ics"},
            diagnostics={
                "versions": self.versions,
                "gates_passed": list(t.gate.gates_passed) if t.gate else [],
                "gates_failed": list(t.gate.gates_failed) if t.gate else [],
                "status": t.status.value,
                "death_reason": t.death_reason,
                "cost_per_side_pts": self.cost_per_side_pts,
                "panel_days": [
                    {"day": d.day, "symbol": d.symbol, "skipped_reason": d.skipped_reason}
                    for d in day_evals
                ],
            },
        )
        return row

    # -- full pipeline -------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        tracked = self.ingest_and_validate()
        compiled = [t for t in tracked if isinstance(t.result, ValidCandidate)]
        logger.info(
            "batch validated",
            run_id=self.rc.run_id,
            candidates=len(tracked),
            compiled=len(compiled),
            invalid=len(tracked) - len(compiled),
        )

        # Per-split day evals: outer days, inner candidates (spec §5 [3]/[4]).
        split_day_evals: dict[str, dict[str, list[DayEval]]] = {}
        for split in SUMMARY_SPLITS:
            split_day_evals[split] = self._evaluate_split_days(compiled, split)

        # [5] reduce per split; EVALUATED status.
        for t in compiled:
            valid = t.result
            assert isinstance(valid, ValidCandidate)
            for split in SUMMARY_SPLITS:
                t.metrics[split] = self._aggregate(valid, split_day_evals[split][valid.alpha_id])
            t.status = Status.EVALUATED
            self._write_candidate_status(t, Status.EVALUATED)

        # [6] gates + final_score + statuses.
        for t in compiled:
            valid = t.result
            assert isinstance(valid, ValidCandidate)
            t.gate = apply_hard_gates(t.metrics["train"], t.metrics["validation"], self.scoring_cfg)
            t.final_score, _ = compute_final_score(
                t.metrics["validation"], valid.signal_node_count, self.scoring_cfg
            )
        scored = [
            ScoredCandidate(
                alpha_id=_alpha_id_of(t.result),
                family=t.result.candidate.family if t.result.candidate else "",
                gate=t.gate,  # type: ignore[arg-type]
                final_score=t.final_score,
                validation_ic_tstat=float(t.metrics["validation"].get("ic_tstat", 0.0)),
            )
            for t in compiled
        ]
        statuses = assign_statuses(scored, self.scoring_cfg)
        for t in compiled:
            alpha_id = _alpha_id_of(t.result)
            t.status = statuses[alpha_id]
            if t.status is Status.REJECTED and t.gate is not None and t.gate.death_reason:
                # Ranking-only rejections (survived gates, below cut) keep ''.
                t.death_reason = t.gate.death_reason.value
            self._write_candidate_status(t, t.status)

        # Result rows + artifacts for train/validation (all compiled candidates).
        result_rows: list[dict[str, Any]] = []
        for t in compiled:
            valid = t.result
            assert isinstance(valid, ValidCandidate)
            for split in SUMMARY_SPLITS:
                row = self._result_row(t, split, t.metrics[split], split_day_evals[split][valid.alpha_id])
                self.writer.write_result_row(row)
                result_rows.append(row)

        # [7] test split: WATCHLIST + PROMOTED only — recorded, never summarized
        # (these rows are NOT appended to result_rows; build_failure_summary
        # drops test rows anyway, belt+braces).
        shortlist = [t for t in compiled if t.status in (Status.WATCHLIST, Status.PROMOTED)]
        if shortlist:
            test_evals = self._evaluate_split_days(shortlist, "test")
            for t in shortlist:
                valid = t.result
                assert isinstance(valid, ValidCandidate)
                t.metrics["test"] = self._aggregate(valid, test_evals[valid.alpha_id])
                row = self._result_row(t, "test", t.metrics["test"], test_evals[valid.alpha_id])
                self.writer.write_result_row(row)

        # [8] failure summary (train+validation only, mechanically).
        candidate_rows = [
            {
                "alpha_id": _alpha_id_of(t.result),
                "family": t.result.candidate.family if t.result.candidate else "",
                "status": t.status.value,
                "death_reason": t.death_reason,
                "detail": t.detail,
                "final_score": t.final_score,
                "proposed_new_primitives": [
                    p.name for p in (t.result.candidate.proposed_new_primitives if t.result.candidate else [])
                ],
            }
            for t in tracked
        ]
        prompt_ids = {
            family_from_filename(Path(t.family_file)): t.provenance["generation_prompt_id"]
            for t in tracked
            if t.provenance.get("generation_prompt_id")
        }
        summary = build_failure_summary(
            run_id=self.rc.run_id,
            versions=self.versions,
            candidate_rows=candidate_rows,
            result_rows=result_rows,
            scoring_cfg=self.scoring_cfg,
            prompt_ids_used=prompt_ids,
        )
        summary_path = write_failure_summary(summary, self.rc.run_dir)
        logger.info(
            "batch run complete",
            run_id=self.rc.run_id,
            totals=summary["totals"],
            summary=str(summary_path),
        )
        return summary


def run_batch(rc: RunConfig, client: Any | None) -> dict[str, Any]:
    """Run one batch end-to-end (CLI: ``run --batch <run_id>``)."""
    return BatchRunner(rc, client).run()


__all__ = [
    "BatchRunner",
    "DEFAULT_CONFIG_DIR",
    "DEFAULT_DATA_ROOT",
    "DEFAULT_RUNS_ROOT",
    "PRIOR_HASHES_SQL",
    "RunConfig",
    "fetch_prior_hashes",
    "run_batch",
]
