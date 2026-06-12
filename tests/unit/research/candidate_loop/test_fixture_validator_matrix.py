"""Spec §17: the 12-candidate fixture covers every validator death reason."""

from __future__ import annotations

from pathlib import Path

from research.candidate_loop.schema import DeathReason
from research.candidate_loop.validator import (
    InvalidCandidate,
    ValidCandidate,
    validate_batch,
)

FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "research"
    / "candidate_loop"
    / "fixtures"
    / "validator_matrix_12.jsonl"
)

VALIDATOR_DEATH_REASONS = (
    DeathReason.SCHEMA_INVALID,
    DeathReason.FORMULA_PARSE_ERROR,
    DeathReason.PRIMITIVE_INVALID,
    DeathReason.UNSUPPORTED_NEW_PRIMITIVE,
    DeathReason.ARGUMENT_INVALID,
    DeathReason.OVER_COMPLEX,
    DeathReason.DUPLICATE_ALPHA,
)


def _results() -> list:
    lines = [ln for ln in FIXTURE.read_text().splitlines() if ln.strip()]
    assert len(lines) == 12
    return validate_batch(lines)


class TestFixtureMatrix:
    def test_five_valid_seven_invalid(self) -> None:
        results = _results()
        valid = [r for r in results if isinstance(r, ValidCandidate)]
        invalid = [r for r in results if isinstance(r, InvalidCandidate)]
        assert len(valid) == 5
        assert len(invalid) == 7

    def test_every_validator_death_reason_covered_exactly_once(self) -> None:
        reasons = [r.death_reason for r in _results() if isinstance(r, InvalidCandidate)]
        assert sorted(r.value for r in reasons) == sorted(r.value for r in VALIDATOR_DEATH_REASONS)

    def test_expected_candidates_die_for_expected_reasons(self) -> None:
        by_name: dict[str, InvalidCandidate] = {}
        for result in _results():
            if isinstance(result, InvalidCandidate) and result.candidate is not None:
                by_name[result.candidate.name] = result
        expected = {
            "bad_family_momentum": DeathReason.SCHEMA_INVALID,
            "bad_parse_unbalanced": DeathReason.FORMULA_PARSE_ERROR,
            "bad_unknown_primitive_vwap": DeathReason.PRIMITIVE_INVALID,
            "bad_uses_proposed_queue_age": DeathReason.UNSUPPORTED_NEW_PRIMITIVE,
            "bad_levels_out_of_range": DeathReason.ARGUMENT_INVALID,
            "bad_nested_too_deep": DeathReason.OVER_COMPLEX,
            "dup_renamed_obi_l3": DeathReason.DUPLICATE_ALPHA,
        }
        assert {n: r.death_reason for n, r in by_name.items()} == expected

    def test_valid_candidates_span_five_families(self) -> None:
        families = {
            r.candidate.family for r in _results() if isinstance(r, ValidCandidate)
        }
        assert families == {
            "microprice",
            "order_book_imbalance",
            "spread_regime",
            "trade_flow",
            "depth_delta",
        }

    def test_trade_flow_candidate_flagged_for_dir_clean_gating(self) -> None:
        tf = [
            r
            for r in _results()
            if isinstance(r, ValidCandidate) and r.candidate.family == "trade_flow"
        ]
        assert len(tf) == 1
        assert tf[0].uses_trade_imbalance is True
