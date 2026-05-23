"""Map each coverage row to exactly one terminal rejection_cause.

Spec Section 2.3. Uses ``max(max_upside_break_pts, max_downside_break_pts)``
for the 8-point gate because ``break_magnitude_pts`` is a first-touch artifact.
"""
from __future__ import annotations

from typing import Mapping

import pandas as pd

REJECTION_CAUSES: tuple[str, ...] = (
    "missing_opening",
    "missing_post",
    "zero_opening_rv",
    "no_break",
    "break_below_8pt",
    "rv_ratio_below_1.25",
    "vwap_filter_fail",
    "would_emit",
)

MIN_BREAK_POINTS = 8.0
MIN_RV_RATIO = 1.25


def _is_missing(value) -> bool:
    """Return True for None or pandas NA/NaN."""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _max_break_pts(row: Mapping) -> float:
    """Qualifying-reach break magnitude based on upside/downside maxima."""
    up = row.get("max_upside_break_pts")
    dn = row.get("max_downside_break_pts")
    up_v = 0.0 if _is_missing(up) else float(up)
    dn_v = 0.0 if _is_missing(dn) else float(dn)
    return max(up_v, dn_v)


def classify_rejection_cause(row: Mapping) -> str:
    """Classify one coverage row into the frozen terminal taxonomy."""
    status = row.get("coverage_status")
    if status == "missing_opening":
        return "missing_opening"
    if status == "missing_post":
        return "missing_post"
    if bool(row.get("event_selected_by_v0")):
        return "would_emit"

    rv_ratio = row.get("realized_vol_ratio")
    mag_vs_rv = row.get("break_magnitude_vs_prior_realized_vol")
    if _is_missing(rv_ratio) and _is_missing(mag_vs_rv):
        return "zero_opening_rv"

    break_side = row.get("break_side")
    if break_side == "none" or _is_missing(break_side):
        return "no_break"

    qualifying_pts = _max_break_pts(row)
    if qualifying_pts < MIN_BREAK_POINTS:
        return "break_below_8pt"

    if _is_missing(rv_ratio) or float(rv_ratio) < MIN_RV_RATIO:
        return "rv_ratio_below_1.25"

    vwap_side = row.get("vwap_side_at_break")
    if break_side == "up" and vwap_side == "below":
        return "vwap_filter_fail"
    if break_side == "down" and vwap_side == "above":
        return "vwap_filter_fail"

    return "would_emit"


def classify_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with ``rejection_cause`` added."""
    out = df.copy()
    out["rejection_cause"] = out.apply(classify_rejection_cause, axis=1)
    return out
