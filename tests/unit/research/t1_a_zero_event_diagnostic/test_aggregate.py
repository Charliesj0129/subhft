from __future__ import annotations

import pandas as pd
import pytest

from research.tools.t1_a_zero_event_diagnostic.aggregate import (
    AggregateResult,
    aggregate,
)
from research.tools.t1_a_zero_event_diagnostic.classify import classify_dataframe
from tests.unit.research.t1_a_zero_event_diagnostic.conftest import coverage_row


def _classify(rows: list[dict]) -> pd.DataFrame:
    return classify_dataframe(pd.DataFrame(rows))


def test_aggregate_histogram_counts():
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
            coverage_status="missing_opening",
            trading_day="2026-04-02",
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
            realized_vol_ratio=1.4,
            vwap_side_at_break="above",
            event_selected_by_v0=True,
            trading_day="2026-04-03",
        ),
    ]
    agg = aggregate(_classify(rows))
    assert isinstance(agg, AggregateResult)
    assert agg.cause_counts["missing_opening"] == 2
    assert agg.cause_counts["would_emit"] == 1
    assert sum(agg.cause_counts.values()) == 3


def test_aggregate_conditional_probabilities_basic():
    rows = [
        coverage_row(
            coverage_status="missing_opening",
            break_side="none",
            max_upside_break_pts=None,
            max_downside_break_pts=None,
            realized_vol_ratio=None,
            break_magnitude_vs_prior_realized_vol=None,
            trading_day="2026-04-01",
        ),
        coverage_row(
            coverage_status="ok",
            break_side="none",
            max_upside_break_pts=2.0,
            max_downside_break_pts=0.0,
            realized_vol_ratio=1.10,
            break_magnitude_vs_prior_realized_vol=0.04,
            trading_day="2026-04-02",
        ),
        coverage_row(
            break_side="up",
            break_magnitude_pts=3.0,
            max_upside_break_pts=5.0,
            max_downside_break_pts=0.0,
            realized_vol_ratio=1.40,
            trading_day="2026-04-03",
        ),
        coverage_row(
            break_side="up",
            break_magnitude_pts=10.0,
            max_upside_break_pts=12.0,
            max_downside_break_pts=0.0,
            realized_vol_ratio=1.4,
            vwap_side_at_break="above",
            event_selected_by_v0=True,
            trading_day="2026-04-04",
        ),
    ]
    probs = aggregate(_classify(rows)).conditional_probs
    assert probs["P_post_present"] == pytest.approx(3 / 4)
    assert probs["P_break_given_post"] == pytest.approx(2 / 3)
    assert probs["P_mag_ge_8_given_break"] == pytest.approx(1 / 2)
    assert probs["P_rv_ratio_ge_1_25_given_break"] == pytest.approx(2 / 2)
    assert probs["P_vwap_ok_given_qualifying"] == pytest.approx(1 / 1)
    assert probs["P_would_emit"] == pytest.approx(1 / 4)


def test_aggregate_conditional_zero_denominator_is_none():
    rows = [
        coverage_row(
            coverage_status="missing_opening",
            break_side="none",
            max_upside_break_pts=None,
            max_downside_break_pts=None,
            realized_vol_ratio=None,
            break_magnitude_vs_prior_realized_vol=None,
        )
    ]
    probs = aggregate(_classify(rows)).conditional_probs
    assert probs["P_post_present"] == pytest.approx(0.0)
    assert probs["P_break_given_post"] is None
    assert probs["P_mag_ge_8_given_break"] is None
    assert probs["P_rv_ratio_ge_1_25_given_break"] is None
    assert probs["P_vwap_ok_given_qualifying"] is None


def test_aggregate_per_contract_per_month_breakdown():
    rows = [
        coverage_row(
            contract="TXFB6",
            trading_day="2026-03-15",
            break_side="up",
            break_magnitude_pts=10.0,
            max_upside_break_pts=12.0,
            max_downside_break_pts=0.0,
            realized_vol_ratio=1.4,
            vwap_side_at_break="above",
            event_selected_by_v0=True,
        ),
        coverage_row(
            contract="TXFB6",
            trading_day="2026-03-16",
            coverage_status="missing_opening",
            break_side="none",
            max_upside_break_pts=None,
            max_downside_break_pts=None,
            realized_vol_ratio=None,
            break_magnitude_vs_prior_realized_vol=None,
        ),
        coverage_row(
            contract="TXFD6",
            trading_day="2026-04-05",
            coverage_status="missing_opening",
            break_side="none",
            max_upside_break_pts=None,
            max_downside_break_pts=None,
            realized_vol_ratio=None,
            break_magnitude_vs_prior_realized_vol=None,
        ),
    ]
    grid = aggregate(_classify(rows)).contract_month_grid
    assert grid[("TXFB6", "2026-03", "would_emit")] == 1
    assert grid[("TXFB6", "2026-03", "missing_opening")] == 1
    assert grid[("TXFD6", "2026-04", "missing_opening")] == 1


def test_aggregate_longest_no_break_trading_day_streak():
    rows = [
        coverage_row(trading_day=f"2026-04-{day:02d}", break_side="none")
        for day in range(1, 16)
    ]
    agg = aggregate(_classify(rows))
    assert agg.longest_no_break_trading_day_streak == 15
