"""Candidate schema contract tests (spec §6): alpha_id, windows, JSON schema."""

from __future__ import annotations

import pytest

from research.candidate_loop.schema import (
    Candidate,
    Feature,
    Window,
    candidate_json_schema,
    canonical_json,
    compute_alpha_id,
    parse_window_spec,
)


def _candidate(**overrides: object) -> Candidate:
    base: dict = {
        "name": "obi_l3_zscore_fade",
        "family": "order_book_imbalance",
        "hypothesis": "Persistent L1-L3 book imbalance predicts short-horizon mid drift.",
        "features": [Feature(name="imb_l3", formula="book_imbalance(3)")],
        "signal_formula": "zscore(imb_l3)",
        "label": "future_mid_return(horizon='1s')",
        "horizon": "1s",
        "expected_sign": "positive",
    }
    base.update(overrides)
    return Candidate(**base)


class TestAlphaId:
    def test_alpha_id_is_16_hex_chars(self) -> None:
        alpha_id = compute_alpha_id(_candidate())
        assert len(alpha_id) == 16
        int(alpha_id, 16)  # raises if not hex

    def test_alpha_id_deterministic_for_identical_content(self) -> None:
        assert compute_alpha_id(_candidate()) == compute_alpha_id(_candidate())

    def test_alpha_id_changes_when_any_field_changes(self) -> None:
        assert compute_alpha_id(_candidate()) != compute_alpha_id(
            _candidate(horizon="2s", label="future_mid_return(horizon='2s')")
        )

    def test_canonical_json_has_sorted_keys_and_no_spaces(self) -> None:
        text = canonical_json(_candidate())
        assert ": " not in text and ", " not in text
        keys = [k for k in ("cost_risk", "expected_sign", "family")]
        positions = [text.index(f'"{k}"') for k in keys]
        assert positions == sorted(positions)


class TestParseWindowSpec:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("2000_events", Window(kind="events", count=2000)),
            ("500ms", Window(kind="time", duration_ns=500_000_000)),
            ("5s", Window(kind="time", duration_ns=5_000_000_000)),
        ],
    )
    def test_valid_specs_parse(self, spec: str, expected: Window) -> None:
        assert parse_window_spec(spec) == expected

    @pytest.mark.parametrize("spec", ["", "events", "5m", "ms500", "5.5s", "-5s", "0s", "0_events"])
    def test_malformed_specs_raise(self, spec: str) -> None:
        with pytest.raises(ValueError):
            parse_window_spec(spec)


class TestJsonSchema:
    def test_schema_marks_required_candidate_fields(self) -> None:
        schema = candidate_json_schema()
        defs = schema.get("$defs", {})
        cand_schema = defs.get("Candidate", schema)
        required = set(cand_schema["required"])
        assert {
            "name",
            "family",
            "hypothesis",
            "features",
            "signal_formula",
            "label",
            "horizon",
            "expected_sign",
        } <= required
        assert "regime_filter" not in required
