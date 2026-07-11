"""failure_summary.json builder (spec §15) — the v1.1 governor's only input.

Hard requirement: the summary is mechanically train+validation only.
``SPLITS_INCLUDED`` is a module constant, the CH fetch SQL hard-codes
``split IN ('train','validation')``, and neither takes a parameter that can
widen it — test-split rows passed in are dropped before any aggregation
(regression-tested).

Approved maker extension: per-family ``maker_cost_failure_rate`` (failed the
``cost_proxy_maker`` gate) and ``maker_rescuable_count`` (failed taker, passed
maker — the pool a maker-execution variant could revive).
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from research.candidate_loop.schema import Status
from research.candidate_loop.scoring import ScoringConfig

SPLITS_INCLUDED: tuple[str, ...] = ("train", "validation")  # FROZEN; never widened

# Hard-coded split filter (spec §15): no parameter exists to widen it.
FETCH_RESULTS_SQL = (
    "SELECT alpha_id, family, split, ic, ic_tstat, final_score, status, "
    "gates_passed, gates_failed, day_count, effective_day_count, "
    "cost_survival_score, maker_cost_survival_score, latency_1ms_score, "
    "one_day_concentration, sign_consistency "
    "FROM research.experiment_results "
    "WHERE run_id = %(run_id)s AND split IN ('train','validation')"
)

_FETCH_COLUMNS = (
    "alpha_id",
    "family",
    "split",
    "ic",
    "ic_tstat",
    "final_score",
    "status",
    "gates_passed",
    "gates_failed",
    "day_count",
    "effective_day_count",
    "cost_survival_score",
    "maker_cost_survival_score",
    "latency_1ms_score",
    "one_day_concentration",
    "sign_consistency",
)


def fetch_result_rows(client: Any, run_id: str) -> list[dict[str, Any]]:
    """Fetch train+validation result rows from CH (split filter is in the SQL)."""
    rows = client.query(FETCH_RESULTS_SQL, parameters={"run_id": run_id}).result_rows
    return [dict(zip(_FETCH_COLUMNS, row)) for row in rows]


def _gate_margin(train_row: dict[str, Any], gate: str, cfg: ScoringConfig) -> float:
    """Signed distance to passing (negative = how far from the threshold)."""
    if gate == "no_signal":
        return abs(float(train_row.get("ic_tstat", 0.0))) - cfg.train_ic_tstat_abs_min
    if gate == "sign_unstable":
        return float(train_row.get("sign_consistency", 0.0)) - cfg.sign_consistency_min
    if gate == "cost_proxy_taker":
        return float(train_row.get("cost_survival_score", 0.0)) - cfg.cost_survival_min
    if gate == "cost_proxy_maker":
        return float(train_row.get("maker_cost_survival_score", 0.0)) - cfg.maker_cost_survival_min
    if gate == "latency_1ms":
        return float(train_row.get("latency_1ms_score", 0.0)) - cfg.latency_retention_min
    if gate == "one_day_only":
        return cfg.one_day_concentration_max - float(train_row.get("one_day_concentration", 1.0))
    return 0.0


def build_failure_summary(
    *,
    run_id: str,
    versions: dict[str, str],
    candidate_rows: list[dict[str, Any]],
    result_rows: list[dict[str, Any]],
    scoring_cfg: ScoringConfig,
    prompt_ids_used: dict[str, str] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the §15 summary dict from per-candidate terminal states + results.

    ``candidate_rows``: one dict per candidate with at least
    ``alpha_id, family, status, death_reason``; optional ``detail``,
    ``proposed_new_primitives``, ``final_score``.
    ``result_rows``: experiment_results-shaped dicts; anything outside
    train/validation is dropped here, unconditionally.
    """
    results = [r for r in result_rows if str(r.get("split", "")) in SPLITS_INCLUDED]
    train_by_alpha = {str(r["alpha_id"]): r for r in results if str(r.get("split")) == "train"}
    val_by_alpha = {str(r["alpha_id"]): r for r in results if str(r.get("split")) == "validation"}

    statuses = [str(c.get("status", "")) for c in candidate_rows]
    totals = {
        "candidates": len(candidate_rows),
        "invalid": statuses.count(Status.INVALID.value),
        "compiled": statuses.count(Status.COMPILED.value),
        "evaluated": statuses.count(Status.EVALUATED.value),
        "rejected": statuses.count(Status.REJECTED.value),
        "watchlist": statuses.count(Status.WATCHLIST.value),
        "promoted": statuses.count(Status.PROMOTED.value),
    }

    per_family: dict[str, dict[str, Any]] = {}
    for family in sorted({str(c.get("family", "")) for c in candidate_rows}):
        cands = [c for c in candidate_rows if str(c.get("family", "")) == family]
        n = len(cands)
        funnel = Counter(str(c.get("status", "")) for c in cands)
        deaths = Counter(
            str(c.get("death_reason", "")) for c in cands if str(c.get("death_reason", ""))
        )
        survivors = [
            c
            for c in cands
            if str(c["alpha_id"]) in train_by_alpha
            and not list(train_by_alpha[str(c["alpha_id"])].get("gates_failed", []))
        ]

        def _gate_rate(gate: str, cands: list[dict[str, Any]] = cands, n: int = n) -> float:
            hit = sum(
                1
                for c in cands
                if gate in list(train_by_alpha.get(str(c["alpha_id"]), {}).get("gates_failed", []))
            )
            return hit / n if n else 0.0

        maker_rescuable = sum(
            1
            for c in cands
            if "cost_proxy_taker"
            in list(train_by_alpha.get(str(c["alpha_id"]), {}).get("gates_failed", []))
            and "cost_proxy_maker"
            not in list(train_by_alpha.get(str(c["alpha_id"]), {}).get("gates_failed", []))
        )

        survivor_ics = [
            float(val_by_alpha[str(c["alpha_id"])].get("ic", 0.0))
            for c in survivors
            if str(c["alpha_id"]) in val_by_alpha
        ]
        if survivor_ics:
            arr = np.asarray(survivor_ics)
            ic_dist = {
                "p10": float(np.quantile(arr, 0.10)),
                "p50": float(np.quantile(arr, 0.50)),
                "p90": float(np.quantile(arr, 0.90)),
            }
        else:
            ic_dist = {"p10": 0.0, "p50": 0.0, "p90": 0.0}

        invalid_details = [
            str(c.get("detail", ""))
            for c in cands
            if str(c.get("status", "")) == Status.INVALID.value and c.get("detail")
        ]
        common_patterns = [text for text, _ in Counter(invalid_details).most_common(3)]

        near_misses = []
        for c in cands:
            train_row = train_by_alpha.get(str(c["alpha_id"]))
            if train_row is None:
                continue
            failed = list(train_row.get("gates_failed", []))
            if len(failed) == 1:
                near_misses.append(
                    {
                        "alpha_id": str(c["alpha_id"]),
                        "failed_gate": failed[0],
                        "margin": _gate_margin(train_row, failed[0], scoring_cfg),
                    }
                )
        near_misses.sort(key=lambda m: -m["margin"])

        reduced_coverage = sum(
            1
            for c in cands
            if str(c["alpha_id"]) in train_by_alpha
            and int(train_by_alpha[str(c["alpha_id"])].get("effective_day_count", 0))
            < int(train_by_alpha[str(c["alpha_id"])].get("day_count", 0))
        )

        per_family[family] = {
            "candidates": n,
            "survival_rate": len(survivors) / n if n else 0.0,
            "status_funnel": {s.value: funnel.get(s.value, 0) for s in Status},
            "death_reason_distribution": dict(sorted(deaths.items())),
            "invalid_formula_rate": (
                deaths.get("FORMULA_PARSE_ERROR", 0) / n if n else 0.0
            ),
            "duplicate_rate": deaths.get("DUPLICATE_ALPHA", 0) / n if n else 0.0,
            "latency_failure_rate": _gate_rate("latency_1ms"),
            "cost_failure_rate": _gate_rate("cost_proxy_taker"),
            "maker_cost_failure_rate": _gate_rate("cost_proxy_maker"),  # extension
            "maker_rescuable_count": maker_rescuable,  # extension
            "reduced_day_coverage_count": reduced_coverage,
            "ic_distribution_survivors": ic_dist,
            "common_failure_patterns": common_patterns,
            "near_misses": near_misses[:5],
        }

    def _terminal_list(status: Status) -> list[dict[str, Any]]:
        rows = [
            {
                "alpha_id": str(c["alpha_id"]),
                "family": str(c.get("family", "")),
                "final_score": float(c.get("final_score", 0.0)),
            }
            for c in candidate_rows
            if str(c.get("status", "")) == status.value
        ]
        return sorted(rows, key=lambda r: -r["final_score"])

    proposed_tally = Counter(
        str(name)
        for c in candidate_rows
        for name in (c.get("proposed_new_primitives") or [])
    )

    return {
        "run_id": run_id,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "versions": dict(versions),
        "splits_included": list(SPLITS_INCLUDED),
        "totals": totals,
        "per_family": per_family,
        "watchlist": _terminal_list(Status.WATCHLIST),
        "promoted": _terminal_list(Status.PROMOTED),
        "proposed_new_primitives_tally": dict(sorted(proposed_tally.items())),
        "prompt_ids_used": dict(prompt_ids_used or {}),
    }


def write_failure_summary(summary: dict[str, Any], run_dir: Path) -> Path:
    path = run_dir / "failure_summary.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True))
    return path


__all__ = [
    "FETCH_RESULTS_SQL",
    "SPLITS_INCLUDED",
    "build_failure_summary",
    "fetch_result_rows",
    "write_failure_summary",
]
