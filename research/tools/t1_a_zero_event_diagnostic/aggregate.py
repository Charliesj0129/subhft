"""Aggregate rejection causes into histograms, probabilities, and grids."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from research.tools.t1_a_zero_event_diagnostic.classify import (
    MIN_BREAK_POINTS,
    MIN_RV_RATIO,
    REJECTION_CAUSES,
    _is_missing,
    _max_break_pts,
)


@dataclass(frozen=True)
class AggregateResult:
    n_total: int
    cause_counts: dict[str, int]
    conditional_probs: dict[str, float | None]
    contract_month_grid: dict[tuple[str, str, str], int]
    per_contract_day_counts: dict[str, int]
    longest_no_break_trading_day_streak: int
    pair_availability_gap_rate: float | None
    would_emit_count_from_coverage: int


def _safe_div(num: float, denom: float) -> float | None:
    return None if denom == 0 else float(num / denom)


def _qualifying_8pt_mask(df: pd.DataFrame) -> pd.Series:
    return df.apply(lambda r: _max_break_pts(r) >= MIN_BREAK_POINTS, axis=1)


def _rv_ge_threshold_mask(df: pd.DataFrame) -> pd.Series:
    def _ok(row) -> bool:
        value = row.get("realized_vol_ratio")
        if _is_missing(value):
            return False
        return float(value) >= MIN_RV_RATIO

    return df.apply(_ok, axis=1)


def _vwap_pass_mask(df: pd.DataFrame) -> pd.Series:
    def _ok(row) -> bool:
        side = row.get("break_side")
        vwap = row.get("vwap_side_at_break")
        if side == "up":
            return vwap == "above"
        if side == "down":
            return vwap == "below"
        return False

    return df.apply(_ok, axis=1)


def _longest_no_break_streak(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    longest = 0
    streak = 0
    ordered = df.sort_values(["trading_day", "contract"], kind="mergesort")
    for row in ordered.to_dict("records"):
        if row.get("break_side") == "none" and row.get("rejection_cause") != "would_emit":
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
    return longest


def aggregate(df: pd.DataFrame) -> AggregateResult:
    """Aggregate a classified dataframe."""
    n_total = int(len(df))
    cause_counts = {cause: 0 for cause in REJECTION_CAUSES}
    if "rejection_cause" in df.columns:
        for cause, count in df["rejection_cause"].value_counts().items():
            cause_counts[str(cause)] = int(count)

    n_missing_opening = cause_counts["missing_opening"]
    n_missing_post = cause_counts["missing_post"]
    n_post_present = n_total - n_missing_opening - n_missing_post

    has_break = df["break_side"].isin(["up", "down"]) if n_total else pd.Series()
    n_break = int(has_break.sum()) if n_total else 0

    qualifying_8 = (_qualifying_8pt_mask(df) & has_break) if n_total else pd.Series()
    n_mag_ge_8 = int(qualifying_8.sum()) if n_total else 0

    rv_ge = (_rv_ge_threshold_mask(df) & has_break) if n_total else pd.Series()
    n_rv_ge_among_breaks = int(rv_ge.sum()) if n_total else 0

    qualifying = (
        _qualifying_8pt_mask(df) & _rv_ge_threshold_mask(df) & has_break
        if n_total
        else pd.Series()
    )
    n_qualifying = int(qualifying.sum()) if n_total else 0
    vwap_pass = (_vwap_pass_mask(df) & qualifying) if n_total else pd.Series()
    n_vwap_pass = int(vwap_pass.sum()) if n_total else 0

    n_would_emit = cause_counts["would_emit"]
    conditional_probs: dict[str, float | None] = {
        "P_post_present": _safe_div(n_post_present, n_total),
        "P_break_given_post": _safe_div(n_break, n_post_present),
        "P_mag_ge_8_given_break": _safe_div(n_mag_ge_8, n_break),
        "P_rv_ratio_ge_1_25_given_break": _safe_div(n_rv_ge_among_breaks, n_break),
        "P_vwap_ok_given_qualifying": _safe_div(n_vwap_pass, n_qualifying),
        "P_would_emit": _safe_div(n_would_emit, n_total),
    }

    grid: dict[tuple[str, str, str], int] = {}
    if n_total:
        trading_day = pd.to_datetime(df["trading_day"], errors="coerce")
        year_month = trading_day.dt.strftime("%Y-%m").fillna("unknown")
        for (contract, ym, cause), count in (
            df.assign(_ym=year_month)
            .groupby(["contract", "_ym", "rejection_cause"])
            .size()
            .items()
        ):
            grid[(str(contract), str(ym), str(cause))] = int(count)

    per_contract_day_counts: dict[str, int] = {}
    if n_total:
        for contract, sub in df.groupby("contract"):
            per_contract_day_counts[str(contract)] = int(sub["trading_day"].nunique())

    return AggregateResult(
        n_total=n_total,
        cause_counts=cause_counts,
        conditional_probs=conditional_probs,
        contract_month_grid=grid,
        per_contract_day_counts=per_contract_day_counts,
        longest_no_break_trading_day_streak=_longest_no_break_streak(df),
        pair_availability_gap_rate=None,
        would_emit_count_from_coverage=n_would_emit,
    )
