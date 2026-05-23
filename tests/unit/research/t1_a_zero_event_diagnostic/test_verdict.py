from __future__ import annotations

from research.tools.t1_a_zero_event_diagnostic.aggregate import AggregateResult
from research.tools.t1_a_zero_event_diagnostic.verdict import (
    THRESHOLDS,
    decide_verdict,
)


def _agg(
    *,
    n_total: int,
    cause_counts: dict[str, int],
    conditional_probs: dict[str, float | None] | None = None,
    per_contract_day_counts: dict[str, int] | None = None,
    contract_month_grid: dict[tuple[str, str, str], int] | None = None,
    longest_no_break_trading_day_streak: int = 0,
    pair_availability_gap_rate: float | None = None,
) -> AggregateResult:
    counts = {
        "missing_opening": 0,
        "missing_post": 0,
        "zero_opening_rv": 0,
        "no_break": 0,
        "break_below_8pt": 0,
        "rv_ratio_below_1.25": 0,
        "vwap_filter_fail": 0,
        "would_emit": 0,
    }
    counts.update(cause_counts)
    return AggregateResult(
        n_total=n_total,
        cause_counts=counts,
        conditional_probs=conditional_probs
        or {
            "P_post_present": 1.0,
            "P_break_given_post": 0.5,
            "P_mag_ge_8_given_break": 0.5,
            "P_rv_ratio_ge_1_25_given_break": 0.5,
            "P_vwap_ok_given_qualifying": 0.5,
            "P_would_emit": 0.0,
        },
        contract_month_grid=contract_month_grid or {},
        per_contract_day_counts=per_contract_day_counts or {},
        longest_no_break_trading_day_streak=longest_no_break_trading_day_streak,
        pair_availability_gap_rate=pair_availability_gap_rate,
        would_emit_count_from_coverage=counts["would_emit"],
    )


def test_verdict_v1_a5_count_divergence():
    agg = _agg(n_total=86, cause_counts={"would_emit": 4})
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DETECTOR_BUG"
    assert out.primary_reason == "A5"
    assert any("A5" in reason for reason in out.reasons)


def test_verdict_v1_a5_primary_when_a1_also_fires():
    agg = _agg(
        n_total=86,
        cause_counts={"would_emit": 4, "missing_opening": 30},
        conditional_probs={
            "P_post_present": 0.65,
            "P_break_given_post": 0.7,
            "P_mag_ge_8_given_break": 0.5,
            "P_rv_ratio_ge_1_25_given_break": 0.5,
            "P_vwap_ok_given_qualifying": 0.5,
            "P_would_emit": 4 / 86,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DETECTOR_BUG"
    assert out.primary_reason == "A5"
    assert any("A1" in reason for reason in out.reasons)
    assert any("A5" in reason for reason in out.reasons)


def test_verdict_v1_a1_alone():
    agg = _agg(
        n_total=100,
        cause_counts={"missing_opening": 30, "no_break": 70},
        conditional_probs={
            "P_post_present": 0.70,
            "P_break_given_post": 0.0,
            "P_mag_ge_8_given_break": None,
            "P_rv_ratio_ge_1_25_given_break": None,
            "P_vwap_ok_given_qualifying": None,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DETECTOR_BUG"
    assert out.primary_reason == "A1"


def test_verdict_v1_a2_alone():
    agg = _agg(
        n_total=100,
        cause_counts={"missing_post": 25, "no_break": 75},
        conditional_probs={
            "P_post_present": 0.75,
            "P_break_given_post": 0.2,
            "P_mag_ge_8_given_break": None,
            "P_rv_ratio_ge_1_25_given_break": None,
            "P_vwap_ok_given_qualifying": None,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DETECTOR_BUG"
    assert out.primary_reason == "A2"


def test_verdict_v1_a3_alone():
    agg = _agg(n_total=100, cause_counts={"zero_opening_rv": 25, "no_break": 75})
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DETECTOR_BUG"
    assert out.primary_reason == "A3"


def test_verdict_v1_a4_low_break_rate_with_n_floor():
    agg = _agg(
        n_total=100,
        cause_counts={"missing_opening": 5, "missing_post": 5, "no_break": 86, "break_below_8pt": 4},
        conditional_probs={
            "P_post_present": 0.90,
            "P_break_given_post": 4 / 90,
            "P_mag_ge_8_given_break": 0.0,
            "P_rv_ratio_ge_1_25_given_break": 1.0,
            "P_vwap_ok_given_qualifying": None,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DETECTOR_BUG"
    assert out.primary_reason == "A4"


def test_verdict_v1_a4_blocked_by_small_n():
    agg = _agg(
        n_total=12,
        cause_counts={"missing_opening": 1, "missing_post": 1, "no_break": 9, "break_below_8pt": 1},
        conditional_probs={
            "P_post_present": 10 / 12,
            "P_break_given_post": 1 / 10,
            "P_mag_ge_8_given_break": 0.0,
            "P_rv_ratio_ge_1_25_given_break": 1.0,
            "P_vwap_ok_given_qualifying": None,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.primary_reason != "A4"


def test_verdict_v2_too_strict_via_8pt():
    agg = _agg(
        n_total=80,
        cause_counts={"break_below_8pt": 40, "no_break": 40},
        conditional_probs={
            "P_post_present": 1.0,
            "P_break_given_post": 40 / 80,
            "P_mag_ge_8_given_break": 4 / 40,
            "P_rv_ratio_ge_1_25_given_break": 0.6,
            "P_vwap_ok_given_qualifying": 0.7,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "V0_RULE_TOO_STRICT"
    assert out.primary_reason == "B3a"


def test_verdict_v2_too_strict_via_rv():
    agg = _agg(
        n_total=80,
        cause_counts={"rv_ratio_below_1.25": 20, "vwap_filter_fail": 5, "no_break": 55},
        conditional_probs={
            "P_post_present": 1.0,
            "P_break_given_post": 25 / 80,
            "P_mag_ge_8_given_break": 1.0,
            "P_rv_ratio_ge_1_25_given_break": 0.20,
            "P_vwap_ok_given_qualifying": 0.0,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "V0_RULE_TOO_STRICT"
    assert out.primary_reason == "B3b"


def test_verdict_v2_blocked_by_b0_floor_on_small_n():
    agg = _agg(
        n_total=10,
        cause_counts={"break_below_8pt": 5, "no_break": 5},
        conditional_probs={
            "P_post_present": 1.0,
            "P_break_given_post": 0.5,
            "P_mag_ge_8_given_break": 0.1,
            "P_rv_ratio_ge_1_25_given_break": 0.5,
            "P_vwap_ok_given_qualifying": 0.5,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict != "V0_RULE_TOO_STRICT"


def test_verdict_v2_b3c_alone_does_not_fire():
    agg = _agg(
        n_total=80,
        cause_counts={"vwap_filter_fail": 5, "no_break": 30, "rv_ratio_below_1.25": 10, "break_below_8pt": 35},
        conditional_probs={
            "P_post_present": 1.0,
            "P_break_given_post": 50 / 80,
            "P_mag_ge_8_given_break": 0.40,
            "P_rv_ratio_ge_1_25_given_break": 0.80,
            "P_vwap_ok_given_qualifying": 0.10,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict != "V0_RULE_TOO_STRICT"
    assert any("B3c" in reason for reason in out.reasons)


def test_verdict_v2_b3c_diagnostic_preserved_when_v3_fires():
    agg = _agg(
        n_total=80,
        cause_counts={"vwap_filter_fail": 5, "no_break": 30, "rv_ratio_below_1.25": 10, "break_below_8pt": 35},
        conditional_probs={
            "P_post_present": 1.0,
            "P_break_given_post": 50 / 80,
            "P_mag_ge_8_given_break": 0.40,
            "P_rv_ratio_ge_1_25_given_break": 0.80,
            "P_vwap_ok_given_qualifying": 0.10,
            "P_would_emit": 0.0,
        },
        per_contract_day_counts={"TXFB6": 5, "TXFD6": 5},
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DATA_COVERAGE_NARROW"
    assert out.primary_reason == "C1"
    assert any("B3c" in reason for reason in out.reasons)


def test_verdict_v2_b3c_combined_with_b3a_fires_with_b3a_primary():
    agg = _agg(
        n_total=80,
        cause_counts={"vwap_filter_fail": 5, "break_below_8pt": 40, "no_break": 30, "rv_ratio_below_1.25": 5},
        conditional_probs={
            "P_post_present": 1.0,
            "P_break_given_post": 50 / 80,
            "P_mag_ge_8_given_break": 0.10,
            "P_rv_ratio_ge_1_25_given_break": 0.6,
            "P_vwap_ok_given_qualifying": 0.10,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "V0_RULE_TOO_STRICT"
    assert out.primary_reason == "B3a"
    assert any("B3c" in reason for reason in out.reasons)


def test_verdict_v3_data_coverage_narrow_c1():
    agg = _agg(
        n_total=40,
        cause_counts={"no_break": 30, "break_below_8pt": 10},
        conditional_probs={
            "P_post_present": 1.0,
            "P_break_given_post": 0.25,
            "P_mag_ge_8_given_break": 0.5,
            "P_rv_ratio_ge_1_25_given_break": 0.5,
            "P_vwap_ok_given_qualifying": 0.5,
            "P_would_emit": 0.0,
        },
        per_contract_day_counts={"TXFB6": 25, "TXFD6": 15, "TXFC6": 18, "TXFE6": 5},
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DATA_COVERAGE_NARROW"
    assert out.primary_reason == "C1"


def test_verdict_v3_data_coverage_narrow_c2():
    agg = _agg(
        n_total=40,
        cause_counts={"no_break": 30, "break_below_8pt": 10},
        conditional_probs={
            "P_post_present": 1.0,
            "P_break_given_post": 0.25,
            "P_mag_ge_8_given_break": 0.5,
            "P_rv_ratio_ge_1_25_given_break": 0.5,
            "P_vwap_ok_given_qualifying": 0.5,
            "P_would_emit": 0.0,
        },
        per_contract_day_counts={"TXFB6": 25, "TXFD6": 25, "TXFC6": 25, "TXFE6": 25},
        pair_availability_gap_rate=0.31,
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DATA_COVERAGE_NARROW"
    assert out.primary_reason == "C2"


def test_verdict_v3_c3_uses_trading_day_sequence_not_calendar():
    agg = _agg(
        n_total=15,
        cause_counts={"no_break": 15},
        conditional_probs={
            "P_post_present": 1.0,
            "P_break_given_post": 0.0,
            "P_mag_ge_8_given_break": None,
            "P_rv_ratio_ge_1_25_given_break": None,
            "P_vwap_ok_given_qualifying": None,
            "P_would_emit": 0.0,
        },
        per_contract_day_counts={"TXFB6": 25, "TXFD6": 25, "TXFC6": 25, "TXFE6": 25},
        longest_no_break_trading_day_streak=15,
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DATA_COVERAGE_NARROW"
    assert out.primary_reason == "C3"


def test_verdict_inconclusive_when_no_rule_fires():
    agg = _agg(
        n_total=40,
        cause_counts={"no_break": 30, "break_below_8pt": 5, "vwap_filter_fail": 5},
        conditional_probs={
            "P_post_present": 1.0,
            "P_break_given_post": 10 / 40,
            "P_mag_ge_8_given_break": 0.6,
            "P_rv_ratio_ge_1_25_given_break": 0.8,
            "P_vwap_ok_given_qualifying": 0.7,
            "P_would_emit": 0.0,
        },
        per_contract_day_counts={"TXFB6": 30, "TXFD6": 30, "TXFC6": 30, "TXFE6": 30},
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "INCONCLUSIVE"


def test_verdict_priority_v1_over_v2_over_v3():
    agg = _agg(
        n_total=100,
        cause_counts={"missing_opening": 30, "break_below_8pt": 30, "no_break": 40},
        conditional_probs={
            "P_post_present": 0.7,
            "P_break_given_post": 30 / 70,
            "P_mag_ge_8_given_break": 0.10,
            "P_rv_ratio_ge_1_25_given_break": 0.8,
            "P_vwap_ok_given_qualifying": 0.8,
            "P_would_emit": 0.0,
        },
        per_contract_day_counts={"TXFB6": 1, "TXFD6": 1, "TXFC6": 1},
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DETECTOR_BUG"
    assert out.primary_reason == "A1"


def test_verdict_thresholds_are_literal_constants():
    assert THRESHOLDS["A1_missing_opening_rate"] == 0.20
    assert THRESHOLDS["A2_missing_post_rate"] == 0.20
    assert THRESHOLDS["A3_zero_rv_rate"] == 0.20
    assert THRESHOLDS["A4_break_rate"] == 0.10
    assert THRESHOLDS["A4_n_post_floor"] == 20
    assert THRESHOLDS["B0_n_post_floor"] == 20
    assert THRESHOLDS["B0_n_break_floor"] == 10
    assert THRESHOLDS["B1_break_rate"] == 0.30
    assert THRESHOLDS["B2_would_emit_rate"] == 0.10
    assert THRESHOLDS["B3a_mag_ge_8_rate"] == 0.20
    assert THRESHOLDS["B3b_rv_rate"] == 0.30
    assert THRESHOLDS["B3b_n_mag_floor"] == 5
    assert THRESHOLDS["B3c_vwap_rate"] == 0.30
    assert THRESHOLDS["B3c_n_qualifying_floor"] == 5
    assert THRESHOLDS["C1_days_per_contract"] == 20
    assert THRESHOLDS["C1_min_contracts_below"] == 2
    assert THRESHOLDS["C2_pair_gap_rate"] == 0.30
    assert THRESHOLDS["C3_consecutive_days"] == 14
