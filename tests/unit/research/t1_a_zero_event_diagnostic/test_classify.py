from __future__ import annotations

import pandas as pd

from research.tools.t1_a_zero_event_diagnostic.classify import (
    REJECTION_CAUSES,
    classify_dataframe,
    classify_rejection_cause,
)
from tests.unit.research.t1_a_zero_event_diagnostic.conftest import coverage_row


def test_classify_row_missing_opening():
    row = coverage_row(
        coverage_status="missing_opening",
        break_side="none",
        max_upside_break_pts=None,
        max_downside_break_pts=None,
        realized_vol_ratio=None,
        break_magnitude_vs_prior_realized_vol=None,
    )
    assert classify_rejection_cause(row) == "missing_opening"


def test_classify_row_missing_post():
    row = coverage_row(
        coverage_status="missing_post",
        break_side="none",
        max_upside_break_pts=None,
        max_downside_break_pts=None,
        realized_vol_ratio=None,
        break_magnitude_vs_prior_realized_vol=None,
    )
    assert classify_rejection_cause(row) == "missing_post"


def test_classify_row_zero_opening_rv():
    row = coverage_row(
        coverage_status="ok",
        break_side="none",
        max_upside_break_pts=0.0,
        max_downside_break_pts=0.0,
        realized_vol_ratio=None,
        break_magnitude_vs_prior_realized_vol=None,
    )
    assert classify_rejection_cause(row) == "zero_opening_rv"


def test_classify_row_no_break():
    row = coverage_row(
        coverage_status="ok",
        break_side="none",
        max_upside_break_pts=0.0,
        max_downside_break_pts=0.0,
        realized_vol_ratio=1.10,
        break_magnitude_vs_prior_realized_vol=0.0,
    )
    assert classify_rejection_cause(row) == "no_break"


def test_classify_row_break_below_8pt_uses_max_not_first_touch():
    row = coverage_row(
        break_side="up",
        break_magnitude_pts=3.0,
        max_upside_break_pts=5.0,
        max_downside_break_pts=0.0,
        realized_vol_ratio=1.40,
    )
    assert classify_rejection_cause(row) == "break_below_8pt"


def test_classify_row_uses_max_break_pts_not_first_touch():
    row = coverage_row(
        break_side="up",
        break_magnitude_pts=3.0,
        max_upside_break_pts=12.0,
        max_downside_break_pts=0.0,
        realized_vol_ratio=1.40,
        vwap_side_at_break="above",
        event_selected_by_v0=True,
    )
    cause = classify_rejection_cause(row)
    assert cause != "break_below_8pt"
    assert cause == "would_emit"


def test_classify_row_trusts_detector_selected_flag_as_single_source_of_truth():
    row = coverage_row(
        break_side="up",
        break_magnitude_pts=1.0,
        max_upside_break_pts=12.0,
        max_downside_break_pts=0.0,
        realized_vol_ratio=1.40,
        vwap_side_at_break="below",
        event_selected_by_v0=True,
    )
    assert classify_rejection_cause(row) == "would_emit"


def test_classify_row_rv_ratio_below():
    row = coverage_row(
        break_side="up",
        break_magnitude_pts=10.0,
        max_upside_break_pts=12.0,
        max_downside_break_pts=0.0,
        realized_vol_ratio=1.00,
    )
    assert classify_rejection_cause(row) == "rv_ratio_below_1.25"


def test_classify_row_vwap_filter_up_below():
    row = coverage_row(
        break_side="up",
        break_magnitude_pts=10.0,
        max_upside_break_pts=12.0,
        max_downside_break_pts=0.0,
        realized_vol_ratio=1.40,
        vwap_side_at_break="below",
    )
    assert classify_rejection_cause(row) == "vwap_filter_fail"


def test_classify_row_vwap_filter_down_above():
    row = coverage_row(
        break_side="down",
        break_magnitude_pts=10.0,
        max_upside_break_pts=0.0,
        max_downside_break_pts=12.0,
        realized_vol_ratio=1.40,
        vwap_side_at_break="above",
    )
    assert classify_rejection_cause(row) == "vwap_filter_fail"


def test_classify_row_would_emit_passes_all_gates():
    row = coverage_row(
        break_side="up",
        break_magnitude_pts=8.0,
        max_upside_break_pts=12.0,
        max_downside_break_pts=0.0,
        realized_vol_ratio=1.50,
        vwap_side_at_break="above",
        event_selected_by_v0=True,
    )
    assert classify_rejection_cause(row) == "would_emit"


def test_classify_row_exhaustive_disjoint():
    rows = [
        coverage_row(
            coverage_status="missing_opening",
            break_side="none",
            max_upside_break_pts=None,
            max_downside_break_pts=None,
            realized_vol_ratio=None,
            break_magnitude_vs_prior_realized_vol=None,
        ),
        coverage_row(
            coverage_status="missing_post",
            break_side="none",
            max_upside_break_pts=None,
            max_downside_break_pts=None,
            realized_vol_ratio=None,
            break_magnitude_vs_prior_realized_vol=None,
        ),
        coverage_row(
            coverage_status="ok",
            break_side="none",
            max_upside_break_pts=0.0,
            max_downside_break_pts=0.0,
            realized_vol_ratio=None,
            break_magnitude_vs_prior_realized_vol=None,
        ),
        coverage_row(
            coverage_status="ok",
            break_side="none",
            max_upside_break_pts=2.0,
            max_downside_break_pts=0.0,
            realized_vol_ratio=1.10,
            break_magnitude_vs_prior_realized_vol=0.04,
        ),
        coverage_row(
            break_side="up",
            break_magnitude_pts=3.0,
            max_upside_break_pts=5.0,
            max_downside_break_pts=0.0,
            realized_vol_ratio=1.40,
        ),
        coverage_row(
            break_side="up",
            break_magnitude_pts=10.0,
            max_upside_break_pts=12.0,
            max_downside_break_pts=0.0,
            realized_vol_ratio=1.00,
        ),
        coverage_row(
            break_side="up",
            break_magnitude_pts=10.0,
            max_upside_break_pts=12.0,
            max_downside_break_pts=0.0,
            realized_vol_ratio=1.40,
            vwap_side_at_break="below",
        ),
        coverage_row(
            break_side="up",
            break_magnitude_pts=8.0,
            max_upside_break_pts=12.0,
            max_downside_break_pts=0.0,
            realized_vol_ratio=1.50,
            vwap_side_at_break="above",
            event_selected_by_v0=True,
        ),
    ]
    causes = [classify_rejection_cause(r) for r in rows]
    assert causes == [
        "missing_opening",
        "missing_post",
        "zero_opening_rv",
        "no_break",
        "break_below_8pt",
        "rv_ratio_below_1.25",
        "vwap_filter_fail",
        "would_emit",
    ]
    for cause in causes:
        assert cause in REJECTION_CAUSES


def test_classify_dataframe_adds_column():
    df = pd.DataFrame(
        [
            coverage_row(
                coverage_status="missing_opening",
                break_side="none",
                max_upside_break_pts=None,
                max_downside_break_pts=None,
                realized_vol_ratio=None,
                break_magnitude_vs_prior_realized_vol=None,
            ),
            coverage_row(
                break_side="up",
                break_magnitude_pts=10.0,
                max_upside_break_pts=12.0,
                max_downside_break_pts=0.0,
                realized_vol_ratio=1.40,
                vwap_side_at_break="above",
                event_selected_by_v0=True,
            ),
        ]
    )
    out = classify_dataframe(df)
    assert "rejection_cause" in out.columns
    assert out["rejection_cause"].tolist() == ["missing_opening", "would_emit"]
