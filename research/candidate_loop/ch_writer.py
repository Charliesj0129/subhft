"""Dual-sink ClickHouse writer for candidate loop rows (spec §8).

Kill-ledger pattern (``src/hft_platform/alpha/kill_ledger.py``): ClickHouse is
the durable sink; every write does an existence pre-check on the dedupe key
(append-only, never UPDATE/DELETE), and any CH failure falls back to
``runs/<run_id>/_results_fallback.jsonl`` with the same dedupe semantics.
``replay_fallback`` flushes the jsonl into CH later (the CLI exposes it).

Dedupe keys:

* ``research.alpha_candidates`` — ``(alpha_id, run_id, status)``: the table is
  an append-only status-transition log; re-running a batch re-emits the same
  transitions, which must not duplicate.
* ``research.experiment_results`` — ``result_id =
  sha256(alpha_id:run_id:split:data_version:evaluator_version:scoring_version)``.

Rows are plain dicts keyed by DDL column names; missing keys get type-correct
defaults so the insert column list always matches the DDL.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from structlog import get_logger

logger = get_logger("candidate_loop_ch_writer")

CANDIDATES_TABLE = "research.alpha_candidates"
RESULTS_TABLE = "research.experiment_results"

CANDIDATE_COLUMNS: tuple[str, ...] = (
    "alpha_id",
    "run_id",
    "name",
    "family",
    "status",
    "death_reason",
    "hypothesis",
    "candidate_json",
    "feature_formulas",
    "signal_formula",
    "label",
    "horizon",
    "expected_sign",
    "regime_filter",
    "formula_hash",
    "uses_trade_imbalance",
    "proposed_new_primitives",
    "generation_model",
    "generation_prompt_id",
    "generation_run_id",
    "data_version",
    "primitive_version",
    "schema_version",
)

RESULT_COLUMNS: tuple[str, ...] = (
    "result_id",
    "experiment_id",
    "run_id",
    "alpha_id",
    "family",
    "split",
    "split_start",
    "split_end",
    "day_count",
    "effective_day_count",
    "ic",
    "rank_ic",
    "ic_tstat",
    "sign_consistency",
    "bucket_spread_pts",
    "bucket_monotonicity",
    "horizon_decay_halflife_ms",
    "day_stability",
    "one_day_concentration",
    "regime_ic_in",
    "regime_ic_out",
    "regime_ic_tight_spread",
    "regime_ic_wide_spread",
    "regime_stability",
    "train_validation_direction_match",
    "validation_test_direction_match",
    "turnover_proxy",
    "gross_pts_per_flip",
    "required_move_threshold_pts",
    "cost_survival_score",
    "maker_fill_prob_mean",
    "maker_required_move_threshold_pts",
    "maker_cost_survival_score",
    "latency_0ms_score",
    "latency_1ms_score",
    "latency_5ms_score",
    "latency_10ms_score",
    "final_score",
    "gates_passed",
    "gates_failed",
    "status",
    "death_reason",
    "artifact_path",
    "data_version",
    "primitive_version",
    "evaluator_version",
    "scoring_version",
    "cost_assumption_version",
    "maker_cost_assumption_version",
    "latency_config_version",
    "generation_model",
    "generation_prompt_id",
    "generation_run_id",
)

_ARRAY_COLUMNS = frozenset({"feature_formulas", "proposed_new_primitives", "gates_passed", "gates_failed"})
_INT_COLUMNS = frozenset(
    {
        "uses_trade_imbalance",
        "day_count",
        "effective_day_count",
        "train_validation_direction_match",
        "validation_test_direction_match",
    }
)
_FLOAT_PREFIXES = ("ic", "rank_ic", "sign_", "bucket_", "horizon_", "day_stability", "one_day", "regime_", "turnover", "gross_", "required_", "cost_survival", "maker_fill", "maker_required", "maker_cost_survival", "latency_", "final_score")
# CH Date columns: the jsonl fallback serializes dates as ISO strings
# (json.dumps default=str), so the replay path must coerce them back.
_DATE_COLUMNS = frozenset({"split_start", "split_end"})
_DATE_EPOCH = date(1970, 1, 1)


def compute_result_id(
    alpha_id: str,
    run_id: str,
    split: str,
    data_version: str,
    evaluator_version: str,
    scoring_version: str,
) -> str:
    payload = f"{alpha_id}:{run_id}:{split}:{data_version}:{evaluator_version}:{scoring_version}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _default_for(column: str) -> Any:
    if column in _ARRAY_COLUMNS:
        return []
    if column in _INT_COLUMNS:
        return 0
    if column in _DATE_COLUMNS:
        return _DATE_EPOCH
    if column.startswith(_FLOAT_PREFIXES):
        return 0.0
    return ""


def _coerce_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        return date.fromisoformat(value)
    return _DATE_EPOCH


def _row_values(row: dict[str, Any], columns: tuple[str, ...]) -> list[Any]:
    values: list[Any] = []
    for col in columns:
        value = row.get(col, _default_for(col))
        if col in _DATE_COLUMNS:
            value = _coerce_date(value)
        values.append(value)
    return values


class ResultWriter:
    """Per-run writer with CH primary + jsonl fallback sinks.

    ``client=None`` means "CH not configured" — everything goes to the
    fallback jsonl (still deduped), and ``replay_fallback`` can flush later.
    """

    def __init__(self, client: Any | None, run_dir: Path) -> None:
        self.client = client
        self.fallback_path = run_dir / "_results_fallback.jsonl"
        self._fallback_keys: set[str] | None = None

    # -- public API ---------------------------------------------------------

    def write_candidate_row(self, row: dict[str, Any]) -> str:
        """Append one alpha_candidates status row; returns 'ch'|'jsonl'|'duplicate'."""
        key = f"cand:{row['alpha_id']}:{row['run_id']}:{row['status']}"
        precheck = (
            f"SELECT count() FROM {CANDIDATES_TABLE} "
            "WHERE alpha_id = %(a)s AND run_id = %(r)s AND status = %(s)s"
        )
        params = {"a": row["alpha_id"], "r": row["run_id"], "s": row["status"]}
        return self._write(CANDIDATES_TABLE, CANDIDATE_COLUMNS, row, key, precheck, params)

    def write_result_row(self, row: dict[str, Any]) -> str:
        """Append one experiment_results row; returns 'ch'|'jsonl'|'duplicate'."""
        key = f"res:{row['result_id']}"
        precheck = f"SELECT count() FROM {RESULTS_TABLE} WHERE result_id = %(id)s"
        return self._write(RESULTS_TABLE, RESULT_COLUMNS, row, key, precheck, {"id": row["result_id"]})

    # -- sinks ----------------------------------------------------------------

    def _write(
        self,
        table: str,
        columns: tuple[str, ...],
        row: dict[str, Any],
        key: str,
        precheck_sql: str,
        precheck_params: dict[str, Any],
    ) -> str:
        if self.client is not None:
            outcome = self._try_ch(table, columns, row, precheck_sql, precheck_params)
            if outcome in ("ch", "duplicate"):
                return outcome
            # CH failed -> fall through to jsonl.
        return self._try_jsonl(table, row, key)

    def _try_ch(
        self,
        table: str,
        columns: tuple[str, ...],
        row: dict[str, Any],
        precheck_sql: str,
        precheck_params: dict[str, Any],
    ) -> str:
        client = self.client
        if client is None:
            return "failed"
        try:
            existing = client.query(precheck_sql, parameters=precheck_params).result_rows
            if existing and existing[0] and int(existing[0][0]) > 0:
                return "duplicate"
            client.insert(table, [_row_values(row, columns)], column_names=list(columns))
        except Exception:  # noqa: BLE001 - any CH failure degrades to jsonl
            logger.warning("candidate_loop CH write failed", table=table, exc_info=True)
            return "failed"
        return "ch"

    def _warm_fallback_keys(self) -> set[str]:
        if self._fallback_keys is not None:
            return self._fallback_keys
        keys: set[str] = set()
        if self.fallback_path.exists():
            for line in self.fallback_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    keys.add(str(json.loads(line).get("dedupe_key", "")))
                except json.JSONDecodeError:
                    continue
        self._fallback_keys = keys
        return keys

    def _try_jsonl(self, table: str, row: dict[str, Any], key: str) -> str:
        keys = self._warm_fallback_keys()
        if key in keys:
            return "duplicate"
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        with self.fallback_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"table": table, "dedupe_key": key, "row": row}, default=str) + "\n")
        keys.add(key)
        return "jsonl"


def replay_fallback(client: Any, fallback_path: Path) -> dict[str, int]:
    """Flush a fallback jsonl into CH (CLI: ``replay-fallback``).

    Re-runs the same dedupe pre-checks, so replaying twice is a no-op.
    Returns counts: inserted / duplicate / failed.
    """
    counts = {"inserted": 0, "duplicate": 0, "failed": 0}
    if not fallback_path.exists():
        return counts
    writer = ResultWriter(client, fallback_path.parent)
    for line in fallback_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            table = str(entry["table"])
            row = dict(entry["row"])
        except (json.JSONDecodeError, KeyError, TypeError):
            counts["failed"] += 1
            continue
        if table == CANDIDATES_TABLE:
            precheck = (
                f"SELECT count() FROM {CANDIDATES_TABLE} "
                "WHERE alpha_id = %(a)s AND run_id = %(r)s AND status = %(s)s"
            )
            params = {"a": row.get("alpha_id"), "r": row.get("run_id"), "s": row.get("status")}
            outcome = writer._try_ch(table, CANDIDATE_COLUMNS, row, precheck, params)
        elif table == RESULTS_TABLE:
            precheck = f"SELECT count() FROM {RESULTS_TABLE} WHERE result_id = %(id)s"
            outcome = writer._try_ch(table, RESULT_COLUMNS, row, precheck, {"id": row.get("result_id")})
        else:
            counts["failed"] += 1
            continue
        if outcome == "ch":
            counts["inserted"] += 1
        elif outcome == "duplicate":
            counts["duplicate"] += 1
        else:
            counts["failed"] += 1
    return counts


__all__ = [
    "CANDIDATES_TABLE",
    "CANDIDATE_COLUMNS",
    "RESULTS_TABLE",
    "RESULT_COLUMNS",
    "ResultWriter",
    "compute_result_id",
    "replay_fallback",
]
