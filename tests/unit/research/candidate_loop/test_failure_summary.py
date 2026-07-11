"""§15 failure summary: totals, family rates, maker extension, NO test-split leakage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.candidate_loop.failure_summary import (
    FETCH_RESULTS_SQL,
    SPLITS_INCLUDED,
    build_failure_summary,
    write_failure_summary,
)
from research.candidate_loop.scoring import load_scoring_config

CFG = load_scoring_config(
    Path(__file__).resolve().parents[4] / "config" / "research" / "candidate_loop" / "scoring_v1.yaml"
)

VERSIONS = {
    "data_version": "txf_l2_2026H1_v1",
    "primitive_version": "prim_v1",
    "evaluator_version": "eval_v1",
    "scoring_version": "score_v1",
    "cost_assumption_version": "taifex_v1",
    "latency_config_version": "lat_shift_v1",
}


def _candidates() -> list[dict]:
    return [
        {
            "alpha_id": "inv1",
            "family": "microprice",
            "status": "INVALID",
            "death_reason": "FORMULA_PARSE_ERROR",
            "detail": "Unmatched '('",
        },
        {
            "alpha_id": "resc",
            "family": "microprice",
            "status": "REJECTED",
            "death_reason": "COST_KILLED",
        },
        {
            "alpha_id": "dead",
            "family": "microprice",
            "status": "REJECTED",
            "death_reason": "COST_KILLED",
        },
        {
            "alpha_id": "win1",
            "family": "microprice",
            "status": "PROMOTED",
            "death_reason": "",
            "final_score": 1.7,
            "proposed_new_primitives": ["cancel_intensity"],
        },
    ]


def _results() -> list[dict]:
    def row(alpha_id: str, split: str, **overrides: object) -> dict:
        base = {
            "alpha_id": alpha_id,
            "family": "microprice",
            "split": split,
            "ic": 0.05,
            "ic_tstat": 2.0,
            "final_score": 1.0,
            "status": "EVALUATED",
            "gates_passed": [],
            "gates_failed": [],
            "day_count": 40,
            "effective_day_count": 40,
            "cost_survival_score": 0.6,
            "maker_cost_survival_score": 0.7,
            "latency_1ms_score": 0.8,
            "one_day_concentration": 0.3,
            "sign_consistency": 0.8,
        }
        base.update(overrides)
        return base

    return [
        # maker-rescuable: taker failed, maker passed (single-gate near miss)
        row("resc", "train", gates_failed=["cost_proxy_taker"], cost_survival_score=0.25),
        row("resc", "validation", gates_failed=["cost_proxy_taker"]),
        # dead under any execution
        row("dead", "train", gates_failed=["cost_proxy_taker", "cost_proxy_maker"]),
        row("dead", "validation", gates_failed=["cost_proxy_taker", "cost_proxy_maker"]),
        # survivor with reduced trade-flow coverage
        row("win1", "train", effective_day_count=27),
        row("win1", "validation", ic=0.08),
        # TEST-SPLIT rows that must never leak (absurd sentinel values)
        row("win1", "test", ic=999.0),
        row("resc", "test", gates_failed=[]),
    ]


@pytest.fixture(scope="module")
def summary() -> dict:
    return build_failure_summary(
        run_id="smoke_001",
        versions=VERSIONS,
        candidate_rows=_candidates(),
        result_rows=_results(),
        scoring_cfg=CFG,
        prompt_ids_used={"microprice": "microprice__v1"},
        generated_at="2026-06-12T00:00:00+00:00",
    )


class TestTestSplitExclusion:
    """Spec §15 regression: the summary is mechanically train+validation only."""

    def test_splits_included_is_frozen(self, summary: dict) -> None:
        assert summary["splits_included"] == ["train", "validation"]
        assert SPLITS_INCLUDED == ("train", "validation")

    def test_test_split_rows_do_not_leak_into_any_stat(self, summary: dict) -> None:
        fam = summary["per_family"]["microprice"]
        # The test row carried ic=999.0; survivor IC distribution must not see it.
        assert fam["ic_distribution_survivors"]["p90"] < 1.0
        # The test row for 'resc' had empty gates_failed; survival still counts
        # only the train-gate survivor (win1).
        assert fam["survival_rate"] == pytest.approx(0.25)

    def test_fetch_sql_hardcodes_split_filter(self) -> None:
        assert "split IN ('train','validation')" in FETCH_RESULTS_SQL
        assert "%(split" not in FETCH_RESULTS_SQL  # no parameter can widen it
        assert "test" not in FETCH_RESULTS_SQL


class TestTotalsAndFamilies:
    def test_totals(self, summary: dict) -> None:
        assert summary["totals"]["candidates"] == 4
        assert summary["totals"]["invalid"] == 1
        assert summary["totals"]["rejected"] == 2
        assert summary["totals"]["promoted"] == 1

    def test_family_rates(self, summary: dict) -> None:
        fam = summary["per_family"]["microprice"]
        assert fam["invalid_formula_rate"] == pytest.approx(0.25)
        assert fam["cost_failure_rate"] == pytest.approx(0.5)  # resc + dead
        assert fam["maker_cost_failure_rate"] == pytest.approx(0.25)  # dead only
        assert fam["maker_rescuable_count"] == 1  # resc
        assert fam["reduced_day_coverage_count"] == 1  # win1 27 < 40
        assert fam["death_reason_distribution"] == {"COST_KILLED": 2, "FORMULA_PARSE_ERROR": 1}
        assert fam["status_funnel"]["PROMOTED"] == 1

    def test_common_failure_patterns_and_near_misses(self, summary: dict) -> None:
        fam = summary["per_family"]["microprice"]
        assert fam["common_failure_patterns"] == ["Unmatched '('"]
        near = {m["alpha_id"]: m for m in fam["near_misses"]}
        assert "resc" in near
        assert near["resc"]["failed_gate"] == "cost_proxy_taker"
        assert near["resc"]["margin"] == pytest.approx(0.25 - 0.3)
        assert "dead" not in near  # two gates failed -> not a near miss

    def test_terminal_lists_and_tally(self, summary: dict) -> None:
        assert summary["promoted"] == [{"alpha_id": "win1", "family": "microprice", "final_score": 1.7}]
        assert summary["watchlist"] == []
        assert summary["proposed_new_primitives_tally"] == {"cancel_intensity": 1}
        assert summary["prompt_ids_used"] == {"microprice": "microprice__v1"}


class TestWrite:
    def test_write_failure_summary_round_trip(self, summary: dict, tmp_path: Path) -> None:
        path = write_failure_summary(summary, tmp_path / "runs" / "smoke_001")
        loaded = json.loads(path.read_text())
        assert loaded["run_id"] == "smoke_001"
        assert loaded["splits_included"] == ["train", "validation"]
