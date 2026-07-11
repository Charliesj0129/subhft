from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research.experiments.validations.t1g_extreme_imbalance_v0.t1g_normal_spread_low_imbalance_rescue import (
    OOS_DATE_CUTOFF,
    RESCUE_MIN_MEAN,
    RESCUE_MIN_N,
    RESCUE_MIN_POSFRAC,
    evaluate_rescue,
    filter_target_cell,
    load_full_rows,
    score,
)


def _row(
    *,
    branch: str = "extreme_low_imbalance_reversal",
    tmf_spread_bucket: str = "normal_2_5",
    date: str = "2026-04-02",
    net_pts: float | None = 10.0,
) -> dict[str, Any]:
    row: dict[str, Any] = {"branch": branch, "tmf_spread_bucket": tmf_spread_bucket, "date": date}
    if net_pts is not None:
        row["label_30m_net_pts"] = net_pts
    return row


def test_score_computes_mean_median_and_positive_fraction_for_mixed_values() -> None:
    result = score([10.0, -5.0, 20.0, 15.0])

    assert result["events"] == 4
    assert result["mean_net_pts"] == 10.0
    assert result["median_net_pts"] == 12.5
    assert result["positive_fraction"] == 0.75


def test_score_returns_none_stats_for_empty_list() -> None:
    result = score([])

    assert result["events"] == 0
    assert result["mean_net_pts"] is None
    assert result["median_net_pts"] is None
    assert result["positive_fraction"] is None


def test_filter_target_cell_excludes_other_branches() -> None:
    rows = [_row(branch="extreme_high_imbalance_momentum"), _row(branch="extreme_low_imbalance_reversal")]

    filtered = filter_target_cell(rows)

    assert len(filtered) == 1
    assert filtered[0]["branch"] == "extreme_low_imbalance_reversal"


def test_filter_target_cell_excludes_other_spread_buckets() -> None:
    rows = [_row(tmf_spread_bucket="wide_gt_5"), _row(tmf_spread_bucket="normal_2_5")]

    filtered = filter_target_cell(rows)

    assert len(filtered) == 1
    assert filtered[0]["tmf_spread_bucket"] == "normal_2_5"


def test_filter_target_cell_excludes_rows_missing_30m_label() -> None:
    rows = [_row(net_pts=None), _row(net_pts=5.0)]

    filtered = filter_target_cell(rows)

    assert len(filtered) == 1
    assert filtered[0]["label_30m_net_pts"] == 5.0


def test_evaluate_rescue_reports_not_rescued_when_full_sample_gate_fails() -> None:
    cell_rows = [_row(date="2026-04-02", net_pts=1.0) for _ in range(RESCUE_MIN_N)]

    result = evaluate_rescue(cell_rows)

    assert result["full_sample"]["events"] == RESCUE_MIN_N
    assert result["full_sample_gate_passes"] is False
    assert result["oos_confirms"] is False
    assert result["verdict"] == "NOT_RESCUED"


def test_evaluate_rescue_flags_data_starved_when_oos_slice_is_tiny() -> None:
    in_sample_rows = [_row(date="2026-04-02", net_pts=20.0) for _ in range(RESCUE_MIN_N - 2)]
    oos_rows = [_row(date="2026-06-03", net_pts=44.0), _row(date="2026-06-04", net_pts=89.0)]
    cell_rows = in_sample_rows + oos_rows

    result = evaluate_rescue(cell_rows)

    assert result["full_sample"]["events"] == RESCUE_MIN_N
    assert result["oos_dated_slice"]["events"] == 2
    assert result["data_starved"] is True
    assert result["data_starved_note"] is not None


def test_evaluate_rescue_full_sample_gate_passes_but_oos_unconfirmed_when_oos_mean_below_floor() -> None:
    in_sample_rows = [_row(date="2026-04-02", net_pts=20.0) for _ in range(RESCUE_MIN_N - 2)]
    weak_oos_rows = [_row(date="2026-06-03", net_pts=1.0), _row(date="2026-06-04", net_pts=-2.0)]
    cell_rows = in_sample_rows + weak_oos_rows

    result = evaluate_rescue(cell_rows)

    assert result["full_sample_gate_passes"] is True
    assert result["oos_confirms"] is False
    assert result["verdict"] == "FULL_SAMPLE_GATE_PASSES_OOS_UNCONFIRMED"


def test_evaluate_rescue_uses_oos_date_cutoff_to_split_slice() -> None:
    cell_rows = [
        _row(date="2026-05-31", net_pts=100.0),
        _row(date=OOS_DATE_CUTOFF, net_pts=-100.0),
    ]

    result = evaluate_rescue(cell_rows)

    assert result["oos_dated_slice"]["events"] == 1
    assert result["oos_dated_slice"]["mean_net_pts"] == -100.0


def test_load_full_rows_reads_full_rows_field_from_json(tmp_path: Path) -> None:
    payload = {"full_rows": [_row(), _row(net_pts=5.0)]}
    path = tmp_path / "labeled_diagnostic.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    rows = load_full_rows(path)

    assert len(rows) == 2


def test_rescue_thresholds_match_t1g_hypothesis_review_established_bar() -> None:
    assert RESCUE_MIN_MEAN == 10.0
    assert RESCUE_MIN_N == 20
    assert RESCUE_MIN_POSFRAC == 0.5
