"""Validator death-reason matrix (spec §13/§17) over the 12-candidate fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.candidate_loop.schema import DeathReason
from research.candidate_loop.validator import (
    InvalidCandidate,
    ValidCandidate,
    compute_formula_hash,
    validate_batch,
    validate_line,
)

FIXTURE = Path(__file__).resolve().parents[4] / "research" / "candidate_loop" / "fixtures" / "validator_matrix_12.jsonl"


def _base_line(**overrides: object) -> str:
    base: dict = {
        "name": "obi_l3_zscore_fade",
        "family": "order_book_imbalance",
        "hypothesis": "Persistent L1-L3 book imbalance predicts short-horizon mid drift.",
        "features": [{"name": "imb_l3", "formula": "book_imbalance(3)"}],
        "signal_formula": "zscore(imb_l3)",
        "label": "future_mid_return(horizon='1s')",
        "horizon": "1s",
        "expected_sign": "positive",
    }
    base.update(overrides)
    return json.dumps(base)


def _validate_one(line: str) -> ValidCandidate | InvalidCandidate:
    return validate_line(line, seen_hashes=set())


class TestFixtureMatrix:
    """The committed 12-candidate fixture covers every validator death reason."""

    @pytest.fixture(scope="class")
    def results(self) -> list:
        lines = FIXTURE.read_text().splitlines()
        assert len(lines) == 12
        return validate_batch(lines)

    def test_first_five_candidates_are_valid(self, results: list) -> None:
        for i in range(5):
            assert isinstance(results[i], ValidCandidate), getattr(results[i], "detail", "")

    @pytest.mark.parametrize(
        ("idx", "reason"),
        [
            (5, DeathReason.SCHEMA_INVALID),
            (6, DeathReason.FORMULA_PARSE_ERROR),
            (7, DeathReason.PRIMITIVE_INVALID),
            (8, DeathReason.UNSUPPORTED_NEW_PRIMITIVE),
            (9, DeathReason.ARGUMENT_INVALID),
            (10, DeathReason.OVER_COMPLEX),
            (11, DeathReason.DUPLICATE_ALPHA),
        ],
    )
    def test_each_invalid_candidate_dies_with_its_reason(self, results: list, idx: int, reason: DeathReason) -> None:
        result = results[idx]
        assert isinstance(result, InvalidCandidate)
        assert result.death_reason == reason, result.detail

    def test_valid_candidates_have_distinct_alpha_ids_and_hashes(self, results: list) -> None:
        valid = [r for r in results if isinstance(r, ValidCandidate)]
        assert len({r.alpha_id for r in valid}) == len(valid)
        assert len({r.formula_hash for r in valid}) == len(valid)

    def test_trade_flow_candidate_flags_trade_imbalance(self, results: list) -> None:
        by_name = {r.candidate.name: r for r in results if isinstance(r, ValidCandidate)}
        assert by_name["trade_flow_imb_clip"].uses_trade_imbalance is True
        assert by_name["obi_l3_zscore_fade"].uses_trade_imbalance is False

    def test_regime_candidate_keeps_compare_ast(self, results: list) -> None:
        by_name = {r.candidate.name: r for r in results if isinstance(r, ValidCandidate)}
        assert by_name["tight_spread_obi_regime"].regime_ast is not None
        assert by_name["obi_l3_zscore_fade"].regime_ast is None


class TestSchemaStage:
    def test_invalid_json_line(self) -> None:
        result = _validate_one("{not json")
        assert isinstance(result, InvalidCandidate)
        assert result.death_reason == DeathReason.SCHEMA_INVALID

    def test_missing_required_field(self) -> None:
        raw = json.loads(_base_line())
        del raw["label"]
        result = _validate_one(json.dumps(raw))
        assert result.death_reason == DeathReason.SCHEMA_INVALID

    def test_unknown_field_rejected(self) -> None:
        raw = json.loads(_base_line())
        raw["surprise"] = 1
        result = _validate_one(json.dumps(raw))
        assert result.death_reason == DeathReason.SCHEMA_INVALID

    def test_hypothesis_too_short(self) -> None:
        result = _validate_one(_base_line(hypothesis="too short"))
        assert result.death_reason == DeathReason.SCHEMA_INVALID

    def test_uppercase_name_rejected(self) -> None:
        result = _validate_one(_base_line(name="ObiFade"))
        assert result.death_reason == DeathReason.SCHEMA_INVALID

    def test_label_horizon_mismatch(self) -> None:
        result = _validate_one(_base_line(label="future_mid_return(horizon='2s')"))
        assert result.death_reason == DeathReason.SCHEMA_INVALID

    def test_label_must_be_future_mid_return(self) -> None:
        result = _validate_one(_base_line(label="mid_price()"))
        assert result.death_reason == DeathReason.SCHEMA_INVALID

    def test_proposed_primitive_claiming_executable_rejected(self) -> None:
        result = _validate_one(
            _base_line(
                proposed_new_primitives=[{"name": "queue_age", "reason": "because", "not_executable_in_v1": False}]
            )
        )
        assert result.death_reason == DeathReason.SCHEMA_INVALID

    def test_zero_features_rejected(self) -> None:
        result = _validate_one(_base_line(features=[]))
        assert result.death_reason == DeathReason.SCHEMA_INVALID

    def test_seven_features_die_as_over_complex(self) -> None:
        feats = [{"name": f"feat_{i}", "formula": f"book_imbalance({(i % 5) + 1})"} for i in range(7)]
        result = _validate_one(_base_line(features=feats, signal_formula="feat_0"))
        assert result.death_reason == DeathReason.OVER_COMPLEX


class TestPrimitiveAndArgumentStages:
    def test_future_mid_return_in_signal_is_primitive_invalid(self) -> None:
        result = _validate_one(_base_line(signal_formula="future_mid_return(horizon='1s')"))
        assert result.death_reason == DeathReason.PRIMITIVE_INVALID

    def test_unknown_identifier_in_signal_is_primitive_invalid(self) -> None:
        result = _validate_one(_base_line(signal_formula="zscore(ghost_feature)"))
        assert result.death_reason == DeathReason.PRIMITIVE_INVALID

    def test_comparison_in_signal_formula_is_parse_error(self) -> None:
        result = _validate_one(_base_line(signal_formula="imb_l3 <= 0"))
        assert result.death_reason == DeathReason.FORMULA_PARSE_ERROR

    def test_regime_without_comparison_is_parse_error(self) -> None:
        result = _validate_one(_base_line(regime_filter="spread_ticks()"))
        assert result.death_reason == DeathReason.FORMULA_PARSE_ERROR

    def test_bad_side_argument(self) -> None:
        result = _validate_one(
            _base_line(features=[{"name": "ds_mid", "formula": "depth_sum('middle', 3)"}], signal_formula="ds_mid")
        )
        assert result.death_reason == DeathReason.ARGUMENT_INVALID

    def test_event_window_below_floor(self) -> None:
        result = _validate_one(_base_line(signal_formula="zscore(imb_l3, window='5_events')"))
        assert result.death_reason == DeathReason.ARGUMENT_INVALID

    def test_time_window_above_ceiling(self) -> None:
        result = _validate_one(
            _base_line(
                features=[{"name": "dd_slow", "formula": "depth_delta('bid', 3, '120s')"}], signal_formula="dd_slow"
            )
        )
        assert result.death_reason == DeathReason.ARGUMENT_INVALID

    def test_horizon_below_time_floor(self) -> None:
        result = _validate_one(_base_line(horizon="50ms", label="future_mid_return(horizon='50ms')"))
        assert result.death_reason == DeathReason.ARGUMENT_INVALID

    def test_clip_lo_not_below_hi(self) -> None:
        result = _validate_one(_base_line(signal_formula="clip(imb_l3, 1, -1)"))
        assert result.death_reason == DeathReason.ARGUMENT_INVALID

    def test_missing_required_window_argument(self) -> None:
        result = _validate_one(_base_line(signal_formula="ema(imb_l3)"))
        assert result.death_reason == DeathReason.ARGUMENT_INVALID

    def test_unknown_keyword_argument(self) -> None:
        result = _validate_one(_base_line(signal_formula="zscore(imb_l3, span='2000_events')"))
        assert result.death_reason == DeathReason.ARGUMENT_INVALID


class TestFormulaHash:
    def test_kwarg_and_positional_forms_collide(self) -> None:
        a = _validate_one(_base_line(signal_formula="zscore(imb_l3, window='2000_events')"))
        b = _validate_one(_base_line(signal_formula="zscore(imb_l3)"))
        assert isinstance(a, ValidCandidate) and isinstance(b, ValidCandidate)
        assert a.formula_hash == b.formula_hash

    def test_equivalent_time_windows_collide(self) -> None:
        a = _validate_one(
            _base_line(features=[{"name": "dd_a", "formula": "depth_delta('bid', 3, '1s')"}], signal_formula="dd_a")
        )
        b = _validate_one(
            _base_line(features=[{"name": "dd_b", "formula": "depth_delta('bid', 3, '1000ms')"}], signal_formula="dd_b")
        )
        assert a.formula_hash == b.formula_hash

    def test_different_horizon_does_not_collide(self) -> None:
        a = _validate_one(_base_line())
        b = _validate_one(_base_line(horizon="2s", label="future_mid_return(horizon='2s')"))
        assert a.formula_hash != b.formula_hash

    def test_regime_filter_changes_hash(self) -> None:
        a = _validate_one(_base_line())
        b = _validate_one(_base_line(regime_filter="spread_ticks() <= 2"))
        assert a.formula_hash != b.formula_hash

    def test_prior_run_hashes_trigger_duplicate(self) -> None:
        first = _validate_one(_base_line())
        assert isinstance(first, ValidCandidate)
        result = validate_line(_base_line(), seen_hashes=set(), prior_hashes=frozenset({first.formula_hash}))
        assert isinstance(result, InvalidCandidate)
        assert result.death_reason == DeathReason.DUPLICATE_ALPHA

    def test_formula_hash_helper_is_deterministic(self) -> None:
        a = _validate_one(_base_line())
        assert isinstance(a, ValidCandidate)
        assert compute_formula_hash(a.signal_ast, a.regime_ast, "1s") == a.formula_hash
