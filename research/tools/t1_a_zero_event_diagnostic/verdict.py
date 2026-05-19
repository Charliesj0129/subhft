"""Pre-registered V1/V2/V3 verdict engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from research.tools.t1_a_zero_event_diagnostic.aggregate import AggregateResult

Verdict = Literal[
    "DETECTOR_BUG",
    "V0_RULE_TOO_STRICT",
    "DATA_COVERAGE_NARROW",
    "INCONCLUSIVE",
]

THRESHOLDS: dict[str, float] = {
    "A1_missing_opening_rate": 0.20,
    "A2_missing_post_rate": 0.20,
    "A3_zero_rv_rate": 0.20,
    "A4_break_rate": 0.10,
    "A4_n_post_floor": 20,
    "B0_n_post_floor": 20,
    "B0_n_break_floor": 10,
    "B1_break_rate": 0.30,
    "B2_would_emit_rate": 0.10,
    "B3a_mag_ge_8_rate": 0.20,
    "B3b_rv_rate": 0.30,
    "B3b_n_mag_floor": 5,
    "B3c_vwap_rate": 0.30,
    "B3c_n_qualifying_floor": 5,
    "C1_days_per_contract": 20,
    "C1_min_contracts_below": 2,
    "C2_pair_gap_rate": 0.30,
    "C3_consecutive_days": 14,
}


@dataclass(frozen=True)
class VerdictResult:
    verdict: Verdict
    primary_reason: str
    reasons: list[str]


def _reason(label: str, msg: str) -> str:
    return f"{label}: {msg}"


def _counts(agg: AggregateResult) -> tuple[int, int, int]:
    n_post = (
        agg.n_total
        - agg.cause_counts["missing_opening"]
        - agg.cause_counts["missing_post"]
    )
    n_break = sum(
        agg.cause_counts[cause]
        for cause in (
            "break_below_8pt",
            "rv_ratio_below_1.25",
            "vwap_filter_fail",
            "would_emit",
        )
    )
    n_mag_ge_8 = sum(
        agg.cause_counts[cause]
        for cause in ("rv_ratio_below_1.25", "vwap_filter_fail", "would_emit")
    )
    return n_post, n_break, n_mag_ge_8


def _v1_reasons(agg: AggregateResult, viability_event_count: int) -> list[str]:
    reasons: list[str] = []
    n = agg.n_total
    p_miss_open = agg.cause_counts["missing_opening"] / n if n else 0.0
    p_miss_post = agg.cause_counts["missing_post"] / n if n else 0.0
    p_zero_rv = agg.cause_counts["zero_opening_rv"] / n if n else 0.0

    coverage_emit = agg.would_emit_count_from_coverage
    if coverage_emit != viability_event_count:
        reasons.append(
            _reason(
                "A5",
                f"coverage would_emit={coverage_emit} != viability events={viability_event_count}",
            )
        )

    if p_miss_open >= THRESHOLDS["A1_missing_opening_rate"]:
        reasons.append(
            _reason(
                "A1",
                "P(missing_opening)="
                f"{p_miss_open:.2%} >= {THRESHOLDS['A1_missing_opening_rate']:.0%}",
            )
        )
    if (
        p_miss_post >= THRESHOLDS["A2_missing_post_rate"]
        and p_miss_open < 0.05
    ):
        reasons.append(
            _reason(
                "A2",
                "P(missing_post)="
                f"{p_miss_post:.2%} >= {THRESHOLDS['A2_missing_post_rate']:.0%} "
                "AND P(missing_opening) < 5%",
            )
        )
    if p_zero_rv >= THRESHOLDS["A3_zero_rv_rate"]:
        reasons.append(
            _reason(
                "A3",
                "P(zero_opening_rv)="
                f"{p_zero_rv:.2%} >= {THRESHOLDS['A3_zero_rv_rate']:.0%}",
            )
        )

    n_post, _n_break, _n_mag = _counts(agg)
    p_break = agg.conditional_probs.get("P_break_given_post")
    if (
        p_break is not None
        and p_break <= THRESHOLDS["A4_break_rate"]
        and p_miss_post < 0.10
        and n_post >= THRESHOLDS["A4_n_post_floor"]
    ):
        reasons.append(
            _reason(
                "A4",
                f"P_break_given_post={p_break:.2%} <= {THRESHOLDS['A4_break_rate']:.0%}, "
                f"N_post_present={n_post} >= {THRESHOLDS['A4_n_post_floor']}",
            )
        )
    return reasons


def _v2_reasons(agg: AggregateResult) -> list[str]:
    n_post, n_break, n_mag_ge_8 = _counts(agg)
    if (
        n_post < THRESHOLDS["B0_n_post_floor"]
        or n_break < THRESHOLDS["B0_n_break_floor"]
    ):
        return []

    p_break = agg.conditional_probs.get("P_break_given_post")
    p_would_emit = agg.conditional_probs.get("P_would_emit") or 0.0
    if p_break is None or p_break < THRESHOLDS["B1_break_rate"]:
        return []
    if not (
        p_would_emit <= THRESHOLDS["B2_would_emit_rate"]
        and agg.cause_counts["would_emit"] == 0
    ):
        return []

    reasons: list[str] = []
    p_mag = agg.conditional_probs.get("P_mag_ge_8_given_break")
    p_rv = agg.conditional_probs.get("P_rv_ratio_ge_1_25_given_break")
    p_vwap = agg.conditional_probs.get("P_vwap_ok_given_qualifying")

    if p_mag is not None and p_mag <= THRESHOLDS["B3a_mag_ge_8_rate"]:
        reasons.append(
            _reason(
                "B3a",
                f"P_mag_ge_8_given_break={p_mag:.2%} <= {THRESHOLDS['B3a_mag_ge_8_rate']:.0%}",
            )
        )
    if (
        p_rv is not None
        and p_rv <= THRESHOLDS["B3b_rv_rate"]
        and n_mag_ge_8 >= THRESHOLDS["B3b_n_mag_floor"]
    ):
        reasons.append(
            _reason(
                "B3b",
                f"P_rv_ratio_ge_1_25_given_break={p_rv:.2%} <= {THRESHOLDS['B3b_rv_rate']:.0%}",
            )
        )

    n_qualifying = agg.cause_counts["vwap_filter_fail"] + agg.cause_counts["would_emit"]
    if (
        p_vwap is not None
        and p_vwap <= THRESHOLDS["B3c_vwap_rate"]
        and n_qualifying >= THRESHOLDS["B3c_n_qualifying_floor"]
    ):
        reasons.append(
            _reason(
                "B3c",
                f"P_vwap_ok_given_qualifying={p_vwap:.2%} <= {THRESHOLDS['B3c_vwap_rate']:.0%} "
                "(diagnostic only)",
            )
        )
    return reasons


def _v3_reasons(agg: AggregateResult) -> list[str]:
    reasons: list[str] = []
    under = [
        contract
        for contract, days in agg.per_contract_day_counts.items()
        if days < THRESHOLDS["C1_days_per_contract"]
    ]
    if len(under) >= THRESHOLDS["C1_min_contracts_below"]:
        reasons.append(
            _reason(
                "C1",
                f"contracts with <{THRESHOLDS['C1_days_per_contract']} days: {under}",
            )
        )
    if (
        agg.pair_availability_gap_rate is not None
        and agg.pair_availability_gap_rate > THRESHOLDS["C2_pair_gap_rate"]
    ):
        reasons.append(
            _reason(
                "C2",
                "pair availability gap rate="
                f"{agg.pair_availability_gap_rate:.2%} > {THRESHOLDS['C2_pair_gap_rate']:.0%}",
            )
        )
    if agg.longest_no_break_trading_day_streak > THRESHOLDS["C3_consecutive_days"]:
        reasons.append(
            _reason(
                "C3",
                "longest trading-day streak with zero would_emit and break_side=none = "
                f"{agg.longest_no_break_trading_day_streak}",
            )
        )
    return reasons


def _primary(reasons: list[str], labels: tuple[str, ...]) -> str:
    for label in labels:
        if any(reason.startswith(f"{label}:") for reason in reasons):
            return label
    return ""


def decide_verdict(agg: AggregateResult, *, viability_event_count: int) -> VerdictResult:
    """Apply V1, then V2, then V3; otherwise INCONCLUSIVE."""
    v1 = _v1_reasons(agg, viability_event_count)
    if v1:
        return VerdictResult("DETECTOR_BUG", _primary(v1, ("A5", "A1", "A2", "A3", "A4")), v1)

    v2 = _v2_reasons(agg)
    primary_v2 = _primary(v2, ("B3a", "B3b"))
    if primary_v2:
        return VerdictResult("V0_RULE_TOO_STRICT", primary_v2, v2)

    v3 = _v3_reasons(agg)
    primary_v3 = _primary(v3, ("C1", "C2", "C3"))
    if primary_v3:
        return VerdictResult("DATA_COVERAGE_NARROW", primary_v3, v2 + v3)

    return VerdictResult("INCONCLUSIVE", "", v2 + v3)
